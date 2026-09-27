# -*- coding: utf-8 -*-
"""
LLM客户端管理器

提供统一的LLM调用接口，按模型等级（level）调用：
- flash: 主力模型（LLMSettings.FLASH，当前统一使用）
- vision: 视觉位（LLMSettings.VISION，当前 = deepseek/deepseek-v4.1-flash，与 flash 同走
  commandcode 中转接入点）
- pro: 高阶模型（LLMSettings.PRO）
- embedding: 嵌入等级（由 VectorConfig 专用链路消费，不走本模块）

模型名与思考开关统一从 config/llm_settings.py 的 LLMSettings 注册表解析。
"""

import inspect
import json
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from pickle import TRUE
import openai
from typing import Dict, Any, List, Optional
from utils.logger import get_logger
from config.llm_settings import LLMConfig, LLMSettings

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


TOKEN_STATS_FILE = Path(__file__).parent.parent / "token_stats.json"
TOKEN_TRACE_FILE = Path(__file__).parent.parent / "token_trace.json"

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
    "deepseek-v4-pro": {
        "input_per_million": 3,
        "output_per_million": 6,
        "cached_per_million": 0.025
    },
    "glm-5.3-flash": {
        "input_per_million": 0.8,
        "output_per_million": 2.8,
        "cached_per_million": 0.23
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
        if use_session_header is None:
            use_session_header = self.is_opencode
        self._session_id = uuid.uuid4().hex
        headers: Dict[str, str] = {"x-opencode-session": self._session_id} if use_session_header else {}
        # extra_headers 用于覆盖 SDK 默认头（如 User-Agent，部分第三方 WAF 拦截 OpenAI SDK UA）
        if extra_headers:
            headers.update(extra_headers)
        self._client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            default_headers=headers or None,
        )
    
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

        response = await self._client.chat.completions.create(**request_params)

        if budget is not None and response.usage is not None:
            budget.record(response.usage.total_tokens)
            if budget.exceeded:
                logger.warning(
                    f"会话 token 预算触顶: {budget.used}/{budget.max_tokens} tokens, "
                    f"下一次调用将被拒绝"
                )

        self._log_usage(response, model, caller_file, caller_name, caller_line)

        return response
    
    def _log_usage(self, response, model: str, caller_file: str, caller_name: str, caller_line: int):
        """记录token消耗"""
        usage = response.usage
        
        cache_hit = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0
        cache_miss = getattr(usage, 'prompt_cache_miss_tokens', 0) or 0
        cached = (getattr(usage.prompt_tokens_details, 'cached_tokens', 0) or 0) if usage.prompt_tokens_details else 0
        reasoning = (usage.completion_tokens_details.reasoning_tokens or 0) if usage.completion_tokens_details else 0
        
        file_display = caller_file.split('\\')[-1] if caller_file else 'unknown'
        
        self._save_stats(file_display, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, 
                        cached, cache_hit, cache_miss, reasoning, model)
    
    def _format_cost(self, value: float) -> str:
        """格式化金额，避免科学计数法"""
        if value == 0:
            return "0.00000000"
        if abs(value) < 0.00000001:
            return f"{value:.10f}".rstrip('0')
        return f"{value:.8f}".rstrip('0').rstrip('.')
    
    def _save_stats(self, file_name: str, prompt: int, completion: int, total: int, cached: int, 
                cache_hit: int, cache_miss: int, reasoning: int, model: str):
        """保存token统计到文件"""
        try:
            stats = {}
            if TOKEN_STATS_FILE.exists():
                with open(TOKEN_STATS_FILE, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
            
            if file_name not in stats:
                stats[file_name] = {
                    "call_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cached_tokens": 0,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost_cache_miss": "0",
                    "cost_cache_hit": "0",
                    "cost_output": "0",
                    "cost_output_content": "0",
                    "cost_output_reasoning": "0",
                    "cost_total": "0",
                    "models": []
                }
            
            pricing = TOKEN_PRICING.get(model, TOKEN_PRICING["deepseek-v4-flash"])
            
            cost_cache_miss = cache_miss / 1_000_000 * pricing["input_per_million"]
            cost_cache_hit = cache_hit / 1_000_000 * pricing["cached_per_million"]
            
            content_tokens = completion - reasoning
            cost_output_content = content_tokens / 1_000_000 * pricing["output_per_million"]
            cost_output_reasoning = reasoning / 1_000_000 * pricing["output_per_million"]
            cost_output = cost_output_content + cost_output_reasoning
            
            cost_total = cost_cache_miss + cost_cache_hit + cost_output
            
            stats[file_name]["call_count"] += 1
            stats[file_name]["prompt_tokens"] += prompt
            stats[file_name]["completion_tokens"] += completion
            stats[file_name]["total_tokens"] += total
            stats[file_name]["cached_tokens"] += cached
            stats[file_name]["cache_hit_tokens"] += cache_hit
            stats[file_name]["cache_miss_tokens"] += cache_miss
            stats[file_name]["reasoning_tokens"] += reasoning
            stats[file_name]["cost_cache_miss"] = self._format_cost(float(stats[file_name].get("cost_cache_miss", "0")) + cost_cache_miss)
            stats[file_name]["cost_cache_hit"] = self._format_cost(float(stats[file_name].get("cost_cache_hit", "0")) + cost_cache_hit)
            stats[file_name]["cost_output"] = self._format_cost(float(stats[file_name].get("cost_output", "0")) + cost_output)
            stats[file_name]["cost_output_content"] = self._format_cost(float(stats[file_name].get("cost_output_content", "0")) + cost_output_content)
            stats[file_name]["cost_output_reasoning"] = self._format_cost(float(stats[file_name].get("cost_output_reasoning", "0")) + cost_output_reasoning)
            stats[file_name]["cost_total"] = self._format_cost(float(stats[file_name].get("cost_total", "0")) + cost_total)
            
            if model not in stats[file_name]["models"]:
                stats[file_name]["models"].append(model)
            
            with open(TOKEN_STATS_FILE, 'w', encoding='utf-8') as f:
                json.dump(stats, f, indent=2, ensure_ascii=False)
            
            if LLMConfig.ENABLE_TOKEN_TRACE:
                self._save_trace(file_name, prompt, completion, total, cached, cache_hit, cache_miss, reasoning, model, 
                               cost_cache_miss, cost_cache_hit, cost_output_content, cost_output_reasoning)
                
        except Exception as e:
            logger.warning(f"保存token统计失败: {e}")
    
    def _save_trace(self, file_name: str, prompt: int, completion: int, total: int, cached: int,
                    cache_hit: int, cache_miss: int, reasoning: int, model: str,
                    cost_cache_miss: float, cost_cache_hit: float, cost_output_content: float, cost_output_reasoning: float):
        """保存token消耗顺序记录"""
        try:
            trace = []
            if TOKEN_TRACE_FILE.exists():
                with open(TOKEN_TRACE_FILE, 'r', encoding='utf-8') as f:
                    trace = json.load(f)
            
            from datetime import datetime
            trace.append({
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "file": file_name,
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "cached_tokens": cached,
                "cache_hit_tokens": cache_hit,
                "cache_miss_tokens": cache_miss,
                "reasoning_tokens": reasoning,
                "cost_cache_miss": self._format_cost(cost_cache_miss),
                "cost_cache_hit": self._format_cost(cost_cache_hit),
                "cost_output_content": self._format_cost(cost_output_content),
                "cost_output_reasoning": self._format_cost(cost_output_reasoning),
                "cost_total": self._format_cost(cost_cache_miss + cost_cache_hit + cost_output_content + cost_output_reasoning)
            })
            
            with open(TOKEN_TRACE_FILE, 'w', encoding='utf-8') as f:
                json.dump(trace, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            logger.warning(f"保存token跟踪失败: {e}")


class LLMBridge:
    """LLM调用桥接器（单例由模块级 llm_bridge 持有）

    模型等级统一从 LLMSettings 注册表解析（flash / conversation / persona /
    pro / vision / embedding），等级同时决定接入点：flash / vision / pro 走主接入点
    （flash 专属 commandcode 中转 DeepSeek V4.1 Flash，未配置时回退 opencode 统一接入点），
    conversation / persona 走各自专属接入点（未配置时回退 opencode 统一接入点）。
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

    def _resolve_conversation_client(self) -> LLMClient:
        """解析 conversation 等级的 client（专属接入点，懒初始化）。

        专用接入点为空时回退 opencode 统一接入点（直接复用主 client）。
        """
        conv_key = LLMSettings.CONVERSATION_API_KEY
        conv_url = LLMSettings.CONVERSATION_BASE_URL
        if not conv_key and not conv_url:
            return self._client
        if self._conversation_client is None:
            self._conversation_client = LLMClient(
                api_key=conv_key or LLMSettings.API_KEY,
                base_url=conv_url or LLMSettings.BASE_URL,
                use_session_header=False,
                # 部分第三方中转 WAF 拦截 OpenAI SDK 默认 UA（403 Your request was blocked），
                # 覆盖为应用自有 UA
                extra_headers={"User-Agent": "N-Touch/1.0"},
            )
        return self._conversation_client

    def _resolve_persona_client(self) -> LLMClient:
        """解析 persona（内心人格）等级的 client（专属接入点，懒初始化）。

        与 conversation 接入点相互独立配置；专用接入点为空时回退
        opencode 统一接入点（直接复用主 client）。
        """
        persona_key = LLMSettings.PERSONA_API_KEY
        persona_url = LLMSettings.PERSONA_BASE_URL
        if not persona_key and not persona_url:
            return self._client
        if self._persona_client is None:
            self._persona_client = LLMClient(
                api_key=persona_key or LLMSettings.API_KEY,
                base_url=persona_url or LLMSettings.BASE_URL,
                use_session_header=False,
                extra_headers={"User-Agent": "N-Touch/1.0"},
            )
        return self._persona_client

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
                - pro: 高阶模型（LLMSettings.PRO，未设置时直接报错阻断）
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

        # conversation 等级路由到专属接入点；persona 等级路由到人格专属接入点；
        # 其余走主接入点（flash 专属 commandcode DeepSeek V4.1 Flash / opencode 统一）
        if level_attr == 'CONVERSATION':
            client = self._resolve_conversation_client()
        elif level_attr == 'PERSONA':
            client = self._resolve_persona_client()
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
