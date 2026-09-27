# -*- coding: utf-8 -*-
"""内心人格召唤 Agent - 按指定人格的立场审视现状，产出该人格视角的思考结论。

取代 advance agent 系统（reorganize / opinion）的换代实现：
理念是 LLM 拥有多重人格，召唤时按 persona_id 从注册表取人格内核提示词，
注入 system prompt 让 LLM 思考链完全代入角色（类似脑内的天使与恶魔）。

召唤时的装配（2026-09-23 起）：
- system prompt = 纯角色描述（kernel_prompt），删除全部公共提示词
  （身份模板 / 输出约束 / 经验缺口声明），输出形态要求移到 user 消息。
- user 消息共用新格式：
    当前你内心的想法是：
    {现状总结}

    你有权反驳该想法并提出你的观点。你的观点要简洁且贴合你自己的口吻。
    匹配到的话术如下，若无匹配，禁止给出发言类建议。
    {匹配到的话术}
- 向量匹配**只对「角色的描述提示词」（kernel_prompt = system prompt）生效**，
  取最相似前 top_k 条话术注入 user 消息末尾供参考；人格仍不派发工具（不主动查询）。

并行处理（2026-09-21/23）：
- 多个内心人格由 PFCA 的 summon_personas 以 asyncio.gather 并行召唤；
- 人格不派发任何工具，单轮直出结论；多轮工具循环不参与。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
  1. 前处理 `_preprocess`    —— 透传已拼装好的 user 消息
  2. LLM 循环 `_llm_loop`    —— 复用父类骨架（level 由人格注册表 model_level 决定，内置人格均为 persona）
  3. 是否继续 `_should_continue` —— 无工具（恒 False），单轮收尾
  4. 工具处理 `_process_tool` —— 无工具不触发
  5. 后处理 `_postprocess`   —— 拼装 persona 信息 + 思考结论
"""

from typing import Dict, List, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.base.persona.persona_registry import get_persona
from utils.logger import get_logger

logger = get_logger('persona_agent', "INFO")


class PersonaAgent(LightweightAgent):
    """内心人格召唤 Agent。

    每次召唤实例化一个，按 persona_id 渲染人格内核 system prompt；
    人格不存在属重大关键路径失败，构造时直接 KeyError 崩溃阻断（user_rules）。
    """

    def __init__(self, persona_id: str, current_summary: str = ""):
        self.persona = get_persona(persona_id)
        self.current_summary = current_summary
        # 事件链上下文前缀按人格粒度区分（persona_assistant / persona_normal / ...），
        # 前端 /context 同一链下显示为多种独立 kind，一眼区分是哪个格
        self.history_prefix = f"persona_{persona_id}"
        super().__init__()

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        # 人格不派发任何工具（2026-09-23 起无 query_bequest）：话术由系统按
        # 角色描述向量匹配注入 user 消息，人格不主动查询，单轮直出结论
        return {}

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return []

    def get_system_prompt(self) -> str:
        """系统提示 = 纯角色描述（kernel_prompt）。

        2026-09-23 起删除全部公共提示词（身份模板 / 输出约束 / 经验缺口声明）：
        system 只含角色本身，输出形态与话术要求全部移到 user 消息新格式（见 run()）。
        """
        return self.persona.kernel_prompt

    # ===================== 五段式骨架 =====================
    def _preprocess(self, query: str) -> Any:
        """前处理：透传已拼装好的 user 消息。"""
        return query

    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        """是否继续：有工具调用则继续执行并回灌；无工具调用（LLM 收尾）则终止。"""
        return bool(response_message.tool_calls)

    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        """后处理：拼装人格信息 + 该人格的思考结论。"""
        return {
            "persona_id": self.persona.persona_id,
            "persona_name": self.persona.name,
            "thought": content,
            "extra_info": extra_info,
        }

    async def _call_llm(self, messages: list, *, level: str = "flash",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm。

        模型等级按人格注册表配置（model_level）：内置三个人格均为 persona
        （内心人格专属接入点，2026-09-23 临时回退主接入点 deepseek-flash），
        未来人格可自行指定 flash 等其它等级。
        人格无工具（2026-09-23 移除 query_bequest 查询经验机制），tools 恒为 None。
        """
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            level=self.persona.model_level,
            messages=full_messages,
            tools=self.get_available_tools() or None,
            tool_choice=tool_choice,
        )

    # ===================== 角色描述向量匹配话术 =====================
    async def _collect_relevant_bequests(self, top_k: int = 3) -> str:
        """按「角色的描述提示词」（kernel_prompt = system prompt）向量化检索经验库。

        2026-09-23 起向量匹配只对角色描述生效：检索 query 就是该人格的角色描述
        （kernel_prompt / system prompt），取最相似的前 top_k 条话术注入 user 消息
        末尾供参考（不派发工具，人格不主动查询）。

        失败语义（user_rules）：嵌入 API 调用失败由检索层原样上抛崩溃阻断，不吞错
        继续；仅当向量嵌入功能显式禁用（generate_vector 返回 None）时检索层返回空，
        本方法随之返回空串（空段过滤，不影响召唤）。
        """
        if not self.persona.kernel_prompt:
            return ""
        from brain.hc.database.bequest import BequestOperations

        bequest_ops = BequestOperations()
        scored = await bequest_ops.search_bequest_by_vector(
            self.persona.kernel_prompt, top_k=top_k, category="speech",
        )
        lines = []
        for bequest_id, sim in scored:
            bequest = await bequest_ops.get_bequest_by_id(bequest_id)
            if not bequest:
                continue
            title = (bequest.get("title") or "").strip()
            summary = (bequest.get("summary") or "").strip()
            if not title or not summary:
                continue
            logger.info(
                f"人格 {self.persona.persona_id} 命中话术: {title} score={sim:.3f}"
            )
            lines.append(f"{title} - {summary}")
        return "\n".join(lines)

    # ===================== 统一入口（三段式骨架） =====================
    async def run(self) -> Dict[str, Any]:
        # user 消息共用新格式（2026-09-23）：
        #   当前你内心的想法是：{现状总结}
        #   你有权反驳该想法并提出你的观点。你的观点要简洁且贴合你自己的口吻。
        #   匹配到的话术如下，若无匹配，禁止给出发言类建议。{匹配到的话术}
        bequests = await self._collect_relevant_bequests()
        query = "\n".join([
            "当前你内心的想法是：",
            self.current_summary,
            "",
            "你有权反驳该想法并提出你的观点。你的观点要简洁且贴合你自己的口吻。",
            "匹配到的话术如下，若无匹配，禁止给出发言类建议。",
            bequests,
        ])

        # 三段式骨架：前处理 → LLM 循环 → 后处理
        logger.info(f"召唤内心人格: {self.persona.persona_id} ({self.persona.name})")
        processed_input = self._preprocess(query)
        loop_result = await self._llm_loop(processed_input)
        content = loop_result.get("content", "")
        extra_info = loop_result.get("extra_info")
        return self._postprocess(content, extra_info)
