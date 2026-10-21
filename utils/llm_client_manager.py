# -*- coding: utf-8 -*-
"""
LLM客户端管理器

提供统一的LLM调用接口，按模型等级（level）调用：
- flash: 主力模型（LLMSettings.FLASH，当前统一使用）
- vision: 视觉位（LLMSettings.VISION，走其专属接入点；未配置时回退主接入点）
- pro: 高阶模型（LLMSettings.PRO）
- embedding: 嵌入等级（由 VectorConfig 专用链路消费，不走本模块）

模型名与思考开关统一从 config/llm_settings.py 的 LLMSettings 注册表解析。
"""

import asyncio
import inspect
import json
import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from pickle import TRUE
import openai
from typing import Dict, Any, List, Optional
from utils.logger import get_logger
from config.llm_settings import LLMConfig, LLMSettings, resolve_provider_label

logger = get_logger('LLMClientManager', log_level='INFO')


class TokenBudgetExceededError(RuntimeError):
    """会话累计 token 预算耗尽。

    注意：故意不继承 AbyssCoreError——预算耗尽是正常运营熔断（优雅停止会话），
    不能触发 @async_handle_errors 的 os._exit 崩溃语义。
    """
    pass


class TokenBudget:
    """单个会话的累计 token 预算（每次调用 usage.total_tokens 的累加）。

    背景（docs/待办.md「空转治本」）：API 无状态，历史每次调用都整段重发计费，
    轮询式死循环的累计消耗随轮数线性上涨——累计预算是对"总花费"的熔断，
    与单次输入窗口上限（管单次大小）和 max_rounds（管轮数）正交互补。
    """

    def __init__(self, max_tokens: int):
        self.max_tokens = int(max_tokens)
        self.used = 0
        self.calls = 0

    def record(self, total_tokens: Optional[int]) -> None:
        """记账一次调用的 total_tokens（None/异常值静默忽略）。"""
        if total_tokens is None or total_tokens < 0:
            return
        self.used += int(total_tokens)
        self.calls += 1

    @property
    def exceeded(self) -> bool:
        return self.used >= self.max_tokens

    def __repr__(self) -> str:
        return f"TokenBudget(used={self.used}/{self.max_tokens}, calls={self.calls})"


# 会话级预算（ContextVar：同一会话 task 内的所有 LLM 调用共享，含工具内部
# 经由 light_agent 等发起的次级调用；asyncio.create_task 会拷贝引用，
# TokenBudget 是可变对象，记账仍落到同一实例）
_TOKEN_BUDGET: ContextVar[Optional[TokenBudget]] = ContextVar("llm_token_budget", default=None)


@contextmanager
def token_budget_scope(budget: Optional[TokenBudget]):
    """在作用域内启用会话 token 预算（嵌套时恢复外层值）。None = 仅透传不启用。"""
    token = _TOKEN_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _TOKEN_BUDGET.reset(token)


# 当前执行中的 agent 身份（ContextVar：同一会话 task 内的 LLM 调用据此归集；
# 轻量 agent 内嵌调用会各自覆盖并复位，归到自身而非外层会话）。
# asyncio.create_task 会拷贝 context，故基类必须在 spawn 子任务前设好作用域。
_CURRENT_AGENT: ContextVar[Optional[str]] = ContextVar("llm_current_agent", default=None)


@contextmanager
def agent_scope(agent: Optional[str]):
    """在作用域内声明当前 agent 身份（嵌套时恢复外层值）。"""
    token = _CURRENT_AGENT.set(agent)
    try:
        yield agent
    finally:
        _CURRENT_AGENT.reset(token)


# 运行期产物归拢到 memory/（经 paths.data_root()，与 memory.db 等用户数据同根：
# 源码运行 = 仓库根；冻结运行 = abyss-data-dir.txt 指定的数据目录，而非 exe 目录）
from paths import data_root as _data_root
TOKEN_STATS_FILE = _data_root() / "memory" / "token_stats.json"
TOKEN_TRACE_FILE = _data_root() / "memory" / "token_trace.json"

# 每百万 token 单价（人民币）。key 为归一化后的模型名（去掉 `deepseek/` 等前缀）。
TOKEN_PRICING = {
    "deepseek-flash": {
        "input_per_million": 1,
        "output_per_million": 2,
        "cached_per_million": 0.02
    },
    "deepseek-v4-flash": {
        "input_per_million": 1,
        "output_per_million": 2,
        "cached_per_million": 0.02
    },
    "deepseek-v4.1-flash": {
        "input_per_million": 1,
        "output_per_million": 2,
        "cached_per_million": 0.02
    },
    # 火山方舟套餐接入点返回的模型名（大小写保留）：同一档价
    "DeepSeek-V4.1-Flash": {
        "input_per_million": 1,
        "output_per_million": 2,
        "cached_per_million": 0.02
    },
    "deepseek-v4-pro": {
        "input_per_million": 3,
        "output_per_million": 6,
        "cached_per_million": 0.025
    },
    "glm-5.3-flash": {
        "input_per_million": 0.8,
        "output_per_million": 2.8,
        "cached_per_million": 0.23
    },
    # croissant 接入点当前停用；单价待确认，占位为 0 避免静默按其它模型误算
    "gemini-3.8-flash": {
        "input_per_million": 0,
        "output_per_million": 0,
        "cached_per_million": 0
    },
    # 火山方舟嵌入（doubao-embedding-vision）——单价待确认，先给占位默认值
    "doubao-embedding-vision": {
        "input_per_million": 0.5,
        "output_per_million": 0,
        "cached_per_million": 0.5
    }
}

# 合法模型等级白名单（LLMSettings 上的等级注册名，大写）
MODEL_LEVELS = ('FLASH', 'CONVERSATION', 'PERSONA', 'PRO', 'VISION', 'EMBEDDING')


def extract_reasoning(message) -> Optional[str]:
    """从 LLM 响应 message 中提取思考链文本（无则 None）。

    兼容不同接入点的字段命名差异（不 try-ex 吞错，缺失属于可选字段的正常情形）：
    - DeepSeek 官方 API：reasoning_content
    - commandcode 中转（DeepSeek V4.1 Flash）：reasoning / reasoning_details
    - opencode 统一接入点：thinking
    """
    if message is None:
        return None
    for attr in ("reasoning_content", "reasoning", "thinking"):
        value = getattr(message, attr, None)
        if isinstance(value, str) and value.strip():
            return value
    details = getattr(message, "reasoning_details", None)
    if isinstance(details, list) and details:
        parts = []
        for d in details:
            text = d.get("text") if isinstance(d, dict) else getattr(d, "text", None)
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return None


class LLMClient:
    """LLM客户端类"""

    def __init__(self, api_key: str = None, base_url: str = None, use_session_header: Optional[bool] = None,
                 extra_headers: Dict[str, str] = None):
        # 接入点参数化：未显式传入时使用 opencode 统一接入点（LLMSettings）
        api_key = api_key or LLMSettings.API_KEY
        base_url = base_url or LLMSettings.BASE_URL
        # opencode Go 网关强制要求会话头（MissingSessionID 400），仅用于网关路由
        # 优化，不进模型上下文。进程级固定 id：路由最稳定，prompt 前缀缓存命中最有保障。
        # 第三方接入点（无该网关约束）不发送；未显式指定时按接入点自动判断。
        self.is_opencode = "opencode" in base_url
        # 接入点信息存档：token 统计按「家（提供商）」维度归集时使用
        self.base_url = base_url
        self.provider = resolve_provider_label(base_url)
        if use_session_header is None:
            use_session_header = self.is_opencode
        self._session_id = uuid.uuid4().hex
        headers: Dict[str, str] = {"x-opencode-session": self._session_id} if use_session_header else {}
        # extra_headers 用于覆盖 SDK 默认头（如 User-Agent，部分第三方 WAF 拦截 OpenAI SDK UA）
        if extra_headers:
            headers.update(extra_headers)
        # 导入工具/启动 Web 时不创建网络客户端；证书及代理初始化留到实际模型调用。
        self._client = None
        self._client_options = dict(api_key=api_key, base_url=base_url, default_headers=headers or None)
    
    async def chat(self, model: str, messages: List[Dict], extra_body: Dict = None,
                  caller_file: str = None, caller_name: str = None, caller_line: int = None, **kwargs):
        """内部聊天方法"""
        # 会话级 token 预算熔断：超限直接拒绝本次调用（拦的是下一轮）
        budget = _TOKEN_BUDGET.get()
        if budget is not None and budget.exceeded:
            raise TokenBudgetExceededError(
                f"会话 token 预算已耗尽: 已用 {budget.used}/{budget.max_tokens} tokens"
            )

        request_params = {
            "model": model,
            "messages": messages,
        }

        if extra_body:
            request_params["extra_body"] = extra_body

        request_params.update(kwargs)

        if self._client is None:
            self._client = openai.AsyncOpenAI(**self._client_options)
        response = await self._client.chat.completions.create(**request_params)

        if budget is not None and response.usage is not None:
            budget.record(response.usage.total_tokens)
            if budget.exceeded:
                logger.warning(
                    f"会话 token 预算触顶: {budget.used}/{budget.max_tokens} tokens, "
                    f"下一次调用将被拒绝"
                )

        agent = _CURRENT_AGENT.get() or self._caller_label(caller_file)
        await record_token_usage(
            agent=agent,
            provider=self.provider,
            model=model,
            usage=response.usage,
        )

        return response

    @staticmethod
    def _caller_label(caller_file: Optional[str]) -> str:
        """无 agent 作用域时（脚本/测试直调）回退到调用方文件名。"""
        if not caller_file:
            return "unknown"
        return caller_file.replace("\\", "/").split("/")[-1]


# ==================================================
# Token 用量统计（观测旁路，按天 / agent / provider / model 分桶）
# ==================================================

# 单进程单事件循环，模块级锁保护「读-改-写」临界区
_STATS_LOCK = asyncio.Lock()


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _empty_bucket() -> Dict[str, Any]:
    return {
        "call_count": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "reasoning_tokens": 0,
        "cost_cache_miss": 0.0,
        "cost_cache_hit": 0.0,
        "cost_output": 0.0,
        "cost_total": 0.0,
        "models": [],
        "providers": [],
    }


def empty_bucket() -> Dict[str, Any]:
    """供读取侧（Web 接口）聚合时构造空桶的公开入口。"""
    return _empty_bucket()


def _load_stats() -> Dict[str, Any]:
    """读取统计文件；非 v2 schema（含旧扁平结构）直接视为空，等价重置。"""
    if not TOKEN_STATS_FILE.exists():
        return {"version": 2, "updated_at": None, "days": {}}
    with open(TOKEN_STATS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("version") != 2:
        return {"version": 2, "updated_at": None, "days": {}}
    data.setdefault("days", {})
    return data


def load_stats() -> Dict[str, Any]:
    """公开只读入口：供 Web 接口读取原始按天统计。"""
    return _load_stats()


def _write_stats(data: Dict[str, Any]) -> None:
    """原子落盘：先写临时文件再 os.replace，避免写入中断损坏统计文件。"""
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    TOKEN_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKEN_STATS_FILE.with_name(TOKEN_STATS_FILE.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, TOKEN_STATS_FILE)


def _compute_cost(model: str, *, completion: int, cache_hit: int, cache_miss: int) -> Dict[str, float]:
    """按归一化模型名查价；未收录模型记 0 花费并告警（不静默按其它模型计价）。"""
    name = model.split("/")[-1] if model and "/" in model else (model or "")
    pricing = TOKEN_PRICING.get(name)
    if pricing is None:
        logger.warning(f"未收录定价的模型: {model}，本次花费记 0，请补录 TOKEN_PRICING")
        return {"cost_cache_miss": 0.0, "cost_cache_hit": 0.0, "cost_output": 0.0, "cost_total": 0.0}
    cost_cache_miss = cache_miss / 1_000_000 * pricing["input_per_million"]
    cost_cache_hit = cache_hit / 1_000_000 * pricing["cached_per_million"]
    cost_output = completion / 1_000_000 * pricing["output_per_million"]
    return {
        "cost_cache_miss": cost_cache_miss,
        "cost_cache_hit": cost_cache_hit,
        "cost_output": cost_output,
        "cost_total": cost_cache_miss + cost_cache_hit + cost_output,
    }


def _accumulate(bucket: Dict[str, Any], *, prompt: int, completion: int, total: int, cached: int,
                cache_hit: int, cache_miss: int, reasoning: int, costs: Dict[str, float],
                model: str, provider: str) -> None:
    bucket["call_count"] += 1
    bucket["prompt_tokens"] += prompt
    bucket["completion_tokens"] += completion
    bucket["total_tokens"] += total
    bucket["cached_tokens"] += cached
    bucket["cache_hit_tokens"] += cache_hit
    bucket["cache_miss_tokens"] += cache_miss
    bucket["reasoning_tokens"] += reasoning
    for key in ("cost_cache_miss", "cost_cache_hit", "cost_output", "cost_total"):
        bucket[key] = round(bucket[key] + costs[key], 8)
    if model and model not in bucket["models"]:
        bucket["models"].append(model)
    if provider and provider not in bucket["providers"]:
        bucket["providers"].append(provider)


def _append_trace(agent: str, provider: str, model: str, prompt: int, completion: int, total: int,
                  cached: int, cache_hit: int, cache_miss: int, reasoning: int,
                  costs: Dict[str, float]) -> None:
    """按调用顺序追写 trace（仅在 LLMConfig.ENABLE_TOKEN_TRACE 时启用）。"""
    try:
        trace = []
        if TOKEN_TRACE_FILE.exists():
            with open(TOKEN_TRACE_FILE, "r", encoding="utf-8") as f:
                trace = json.load(f)
        trace.append({
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "agent": agent,
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cached_tokens": cached,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "reasoning_tokens": reasoning,
            "cost_total": round(costs["cost_total"], 8),
        })
        with open(TOKEN_TRACE_FILE, "w", encoding="utf-8") as f:
            json.dump(trace, f, indent=2, ensure_ascii=False)
    except (OSError, json.JSONDecodeError):
        logger.exception("token 跟踪写入失败（非关键路径，已忽略）")


async def _record_usage(agent: str, provider: str, model: str, *, prompt: int, completion: int,
                        total: int, cached: int, cache_hit: int, cache_miss: int,
                        reasoning: int) -> None:
    """观测旁路：按天 / agent / provider / model 分桶累计一次调用用量。

    统计不参与业务结果，属非关键路径——仅在持久化本身失败时记日志，
    绝不阻断 LLM 调用返回（窄捕获，不用 except Exception 掩盖编程错误）。
    """
    agent_key = agent or "unknown"
    provider_key = provider or "未知接入点"
    model_key = model or "unknown"
    costs = _compute_cost(model_key, completion=completion, cache_hit=cache_hit, cache_miss=cache_miss)
    try:
        async with _STATS_LOCK:
            data = _load_stats()
            day = data["days"].setdefault(
                _today_str(), {"by_agent": {}, "by_provider": {}, "by_model": {}}
            )
            for dim, key in (("by_agent", agent_key), ("by_provider", provider_key), ("by_model", model_key)):
                bucket = day[dim].setdefault(key, _empty_bucket())
                _accumulate(bucket, prompt=prompt, completion=completion, total=total, cached=cached,
                            cache_hit=cache_hit, cache_miss=cache_miss, reasoning=reasoning,
                            costs=costs, model=model_key, provider=provider_key)
            _write_stats(data)
    except (OSError, json.JSONDecodeError):
        logger.exception("token 统计写入失败（非关键路径，已忽略）")
        return

    if LLMConfig.ENABLE_TOKEN_TRACE:
        _append_trace(agent_key, provider_key, model_key, prompt, completion, total,
                      cached, cache_hit, cache_miss, reasoning, costs)


async def record_token_usage(*, agent: str, provider: str, model: str, usage) -> None:
    """记录一次 chat 调用用量（usage 为空则跳过）。"""
    if usage is None:
        return
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(getattr(usage, "total_tokens", 0) or 0)
    cache_hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    cache_miss = int(getattr(usage, "prompt_cache_miss_tokens", 0) or 0)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached = int(getattr(prompt_details, "cached_tokens", 0) or 0) if prompt_details else 0
    # 火山方舟等 OpenAI 兼容接入点不透传 DeepSeek 原生缓存字段
    # （prompt_cache_hit_tokens / prompt_cache_miss_tokens 恒缺省），只在
    # prompt_tokens_details.cached_tokens 里给出命中量。原生字段缺失时以 cached 回填，
    # 否则命中率恒为 0，且输入花费按全价（未命中价）误算。
    if cache_hit == 0 and cache_miss == 0 and cached > 0:
        cache_hit = cached
        cache_miss = max(prompt - cached, 0)
    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning = int(getattr(completion_details, "reasoning_tokens", 0) or 0) if completion_details else 0
    await _record_usage(agent, provider, model, prompt=prompt, completion=completion, total=total,
                        cached=cached, cache_hit=cache_hit, cache_miss=cache_miss, reasoning=reasoning)


async def record_embedding_usage(*, model: str, provider: str, usage: Optional[Dict[str, Any]]) -> None:
    """记录一次嵌入调用用量（火山 /embeddings 的 usage 可能缺失，缺失则跳过）。"""
    if not usage:
        return
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    total = int(usage.get("total_tokens", prompt) or 0)
    await _record_usage("embedding", provider, model, prompt=prompt, completion=0, total=total,
                        cached=0, cache_hit=0, cache_miss=prompt, reasoning=0)


class LLMBridge:
    """LLM调用桥接器（单例由模块级 llm_bridge 持有）

    模型等级统一从 LLMSettings 注册表解析（flash / conversation / persona /
    pro / vision / embedding），等级同时决定接入点：flash / pro 走主接入点
    （flash 专属 commandcode 中转 DeepSeek V4.1 Flash，未配置时回退 opencode 统一接入点），
    conversation / persona / vision 走各自专属接入点（未配置时回退主接入点）。
    思考开关由 LLMSettings.THINKING 全局控制（仅 opencode 接入点注入 thinking
    extra_body，第三方接入点不注入——commandcode 接入点走其默认值，默认即思考模式）。
    """

    def __init__(self):
        # 主 client 服务 flash / vision / pro 等级：优先走主力专属接入点
        # （commandcode 中转 DeepSeek V4.1 Flash），未配置时 LLMClient 内部回退 opencode 统一接入点
        self._client = LLMClient(
            api_key=LLMSettings.FLASH_API_KEY or None,
            base_url=LLMSettings.FLASH_BASE_URL or None,
            # 第三方中转 WAF 可能拦截 OpenAI SDK 默认 UA，覆盖为应用自有 UA
            extra_headers={"User-Agent": "N-Touch/1.0"},
        )
        # conversation 专属 client（接入点配置为空时与主 client 等价，共享实例）
        self._conversation_client = None
        # persona（内心人格）专属 client（接入点配置为空时与主 client 等价）
        self._persona_client = None
        # vision（视觉位）专属 client（接入点配置为空时与主 client 等价）
        self._vision_client = None

    def _resolve_conversation_client(self) -> LLMClient:
        """解析 conversation 等级的 client（专属接入点，懒初始化）。

        专用接入点为空时复用 common；只覆盖部分字段时，其余字段继承 common。
        """
        conv_key = LLMSettings.CONVERSATION_API_KEY
        conv_url = LLMSettings.CONVERSATION_BASE_URL
        if not conv_key and not conv_url:
            return self._client
        if self._conversation_client is None:
            self._conversation_client = LLMClient(
                api_key=conv_key or LLMSettings.FLASH_API_KEY or LLMSettings.API_KEY,
                base_url=conv_url or LLMSettings.FLASH_BASE_URL or LLMSettings.BASE_URL,
                use_session_header=False,
                # 部分第三方中转 WAF 拦截 OpenAI SDK 默认 UA（403 Your request was blocked），
                # 覆盖为应用自有 UA
                extra_headers={"User-Agent": "N-Touch/1.0"},
            )
        return self._conversation_client

    def _resolve_persona_client(self) -> LLMClient:
        """解析 persona（内心人格）等级的 client（专属接入点，懒初始化）。

        与 conversation 相互独立；空字段继承 common，接入点皆空时复用主 client。
        """
        persona_key = LLMSettings.PERSONA_API_KEY
        persona_url = LLMSettings.PERSONA_BASE_URL
        if not persona_key and not persona_url:
            return self._client
        if self._persona_client is None:
            self._persona_client = LLMClient(
                api_key=persona_key or LLMSettings.FLASH_API_KEY or LLMSettings.API_KEY,
                base_url=persona_url or LLMSettings.FLASH_BASE_URL or LLMSettings.BASE_URL,
                use_session_header=False,
                extra_headers={"User-Agent": "N-Touch/1.0"},
            )
        return self._persona_client

    def _resolve_vision_client(self) -> LLMClient:
        """解析 vision（视觉位）等级的 client（专属接入点，懒初始化）。

        与主力/会话/人格接入点相互独立配置；专用接入点为空时回退主接入点
        （直接复用主 client）。
        """
        vision_key = LLMSettings.VISION_API_KEY
        vision_url = LLMSettings.VISION_BASE_URL
        if not vision_key and not vision_url:
            return self._client
        if self._vision_client is None:
            self._vision_client = LLMClient(
                api_key=vision_key or LLMSettings.FLASH_API_KEY or LLMSettings.API_KEY,
                base_url=vision_url or LLMSettings.FLASH_BASE_URL or LLMSettings.BASE_URL,
                use_session_header=False,
                extra_headers={"User-Agent": "N-Touch/1.0"},
            )
        return self._vision_client

    async def call_llm(
        self,
        level: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        caller_file: str = None,
        caller_name: str = None,
        caller_line: int = None,
        **kwargs
    ) -> Any:
        """
        调用LLM的统一接口

        Args:
            level: 模型等级，可选值：
                - flash: 主力模型（LLMSettings.FLASH）
                - conversation: conversation 会话模型（LLMSettings.CONVERSATION，专属接入点）
                - pro: 高阶模型（未单独配置时继承 common）
                - vision / embedding: 视觉 / 嵌入等级
            messages: 消息列表 (必需)
            tools: 工具列表 (可选)
            **kwargs: 其他传递给API的参数

        Returns:
            LLM响应对象
        """
        level_attr = level.upper()
        if level_attr not in MODEL_LEVELS:
            raise ValueError(
                f"未知的模型等级: {level}，支持的等级: flash / conversation / pro / vision / embedding"
            )
        model = getattr(LLMSettings, level_attr)
        if not model:
            raise ValueError(f"模型等级 {level} 尚未在 LLMSettings 中设置模型，禁止降级调用")

        # conversation / persona / vision 等级各自路由到专属接入点；
        # 其余走主接入点（flash 专属 commandcode DeepSeek V4.1 Flash / opencode 统一）
        if level_attr == 'CONVERSATION':
            client = self._resolve_conversation_client()
        elif level_attr == 'PERSONA':
            client = self._resolve_persona_client()
        elif level_attr == 'VISION':
            client = self._resolve_vision_client()
        else:
            client = self._client
        is_opencode = client.is_opencode

        # thinking extra_body 为 opencode 接入点思考开关格式，第三方接入点不注入
        extra_body = None
        if is_opencode:
            extra_body = {"thinking": {"type": "enabled" if LLMSettings.THINKING else "disabled"}}

        request_params = {
            "model": model,
            "messages": messages,
        }

        if tools is not None:
            request_params["tools"] = tools

        request_params.update(kwargs)

        logger.debug(f"LLM调用 - 等级: {level}, 模型: {model}, 思考: {LLMSettings.THINKING if is_opencode else '由接入点默认'}")

        return await client.chat(**request_params, extra_body=extra_body,
                                 caller_file=caller_file, caller_name=caller_name, caller_line=caller_line)


llm_bridge = LLMBridge()


async def call_llm(
    level: str = "flash",
    messages: List[Dict[str, Any]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    **kwargs
) -> Any:
    """
    调用LLM的统一接口

    Args:
        level: 模型等级（flash / pro / vision / embedding），默认 flash
        messages: 消息列表 (必需)
        tools: 工具列表 (可选)
        **kwargs: 其他传递给API的参数

    Returns:
        LLM响应对象
    """
    caller_frame = inspect.currentframe().f_back
    caller_info = caller_frame.f_code
    caller_file = caller_info.co_filename
    caller_name = caller_info.co_name
    caller_line = caller_frame.f_lineno

    return await llm_bridge.call_llm(level, messages, tools, caller_file=caller_file, caller_name=caller_name, caller_line=caller_line, **kwargs)
