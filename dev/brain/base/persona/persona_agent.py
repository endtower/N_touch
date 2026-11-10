# -*- coding: utf-8 -*-
"""内心人格自荐 Agent - 按指定人格的立场审视现状，给出参与度评分。

取代 advance agent 系统（reorganize / opinion）的换代实现：
理念是 LLM 拥有多重人格，召唤时按 persona_name 从注册表取人格内核提示词，
注入 system prompt 让 LLM 思考链完全代入角色（类似脑内的天使与恶魔）。

内荐机制（2026-09-28；2026-09-29 去掉人格替换）：
人格不再产出"思考结论"供 PFCA 选用，而是各自就当前现状给出**参与度评分**
（0-100，越高越该由自己上场），并附一句若上场将采取的行动方向与思路供参考；
由 PFCA 的 summon_personas 汇总后返回各人格评分与行动方向，供 PFCA 决策参考，
不再切换任何人格（人格改为逐会话由工具入参显式指定）。

召唤时的装配：
- system prompt = 纯角色描述（kernel_prompt），删除全部公共提示词
  （身份模板 / 输出约束 / 经验缺口声明），输出形态要求移到 user 消息。
- user 消息共用格式：
    当前现状：
    {现状总结}
    每轴【当前情绪】{轴名}：数值与感受（与决策侧一致）
    该轴最近五条：{事件标题} {评价标签} {分数变化}

    请以你的人格立场审视该现状，判断你介入处理此事的适配程度，给出参与度评分。
    评分规则：0-100 的整数，越高表示越该由你上场。
    输出格式（严格三行，不要多余内容）：
    评分：<0-100 的整数>
    理由：<一句话理由>
    行动：<一句话：若你上场，将采取的行动方向与思路>
- 人格不派发任何工具，也不做经验向量匹配（2026-09-28 移除话术参考）：
  仅按自身角色描述审视现状给出评分，不主动查询、不注入经验。

并行处理：
- 全部内心人格由 PFCA 的 summon_personas 以 asyncio.gather 并行自荐；
- 人格不派发任何工具，单轮直出评分；多轮工具循环不参与。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
  1. 前处理 `_preprocess`    —— 透传已拼装好的 user 消息
  2. LLM 循环 `_llm_loop`    —— 复用父类骨架（level 由人格注册表 model_level 决定，内置人格均为 persona）
  3. 是否继续 `_should_continue` —— 无工具（恒 False），单轮收尾
  4. 工具处理 `_process_tool` —— 无工具不触发
  5. 后处理 `_postprocess`   —— 解析参与度评分 + 拼装 persona 信息
"""

import re
from typing import Dict, List, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.base.persona.persona_registry import PersonaDef
from brain.amy.feedback_state import get_feedback_state
from brain.amy.decision_feelings import build_decision_feedback_prompt
from utils.logger import get_logger

logger = get_logger('persona_agent', "INFO")

# 参与度评分解析：严格取"评分：<整数>"行；解析不出视为关键产出缺失，直接抛错阻断
_SCORE_PATTERN = re.compile(r"评分[:：]\s*(\d{1,3})")
# 行动方向解析：取"行动：<文字>"行（宽松，缺失时返回空串，不阻断）
_PLAN_PATTERN = re.compile(r"行动[:：]\s*(.+)")


class PersonaAgent(LightweightAgent):
    """内心人格自荐 Agent。

    每次召唤使用当时的人格定义渲染 system prompt，产出该人格对现状的参与度评分。
    """

    def __init__(self, persona: PersonaDef, current_summary: str = ""):
        self.persona = persona
        self.current_summary = current_summary
        # 事件链上下文前缀按人格粒度区分（persona_assistant / persona_normal / ...），
        # 前端 /context 同一链下显示为多种独立 kind，一眼区分是哪个格
        self.history_prefix = f"persona_{persona.persona_name}"
        super().__init__()

    def _initialize_tool_functions(self) -> Dict[str, Any]:
        # 人格不派发任何工具，也不注入经验话术（2026-09-28 移除经验向量匹配）：
        # 只按自身角色描述单轮直出评分，不主动查询、不使用经验
        return {}

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return []

    def get_system_prompt(self) -> str:
        """系统提示 = 纯角色描述（kernel_prompt）。

        2026-09-23 起删除全部公共提示词（身份模板 / 输出约束 / 经验缺口声明）：
        system 只含角色本身，输出形态要求全部移到 user 消息新格式（见 run()）。
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
        """后处理：解析该人格给出的参与度评分 + 拼装人格信息。

        评分解析不出（人格未按格式输出）视为关键产出缺失，直接抛 ValueError
        上抛（经工具层可恢复通道回给 LLM），不吞错继续（user_rules）。
        """
        score = self._parse_score(content)
        return {
            "persona_name": self.persona.persona_name,
            "score": score,
            "reason": (content or "").strip(),
            "plan": self._parse_plan(content),
            "extra_info": extra_info,
        }

    @staticmethod
    def _parse_score(content: str) -> int:
        """从人格输出中解析参与度评分（0-100 整数）。"""
        match = _SCORE_PATTERN.search(content or "")
        if not match:
            raise ValueError(
                f"人格未给出可解析的参与度评分（缺少「评分：<整数>」行）: {content!r}"
            )
        score = int(match.group(1))
        if not 0 <= score <= 100:
            raise ValueError(f"人格参与度评分越界（应为 0-100）: {score}")
        return score

    @staticmethod
    def _parse_plan(content: str) -> str:
        """从人格输出中解析"行动"方向与思路（宽松，缺失返回空串）。"""
        match = _PLAN_PATTERN.search(content or "")
        return match.group(1).strip() if match else ""

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

    # ===================== 统一入口（三段式骨架） =====================
    async def run(self) -> Dict[str, Any]:
        # user 消息共用格式（内荐，2026-09-28）：
        #   当前现状：{现状总结}
        #   双轴感受与归因（格式与决策侧一致）：
        #     每轴【当前情绪】{轴名}：数值与感受，紧随该轴最近五条
        #     「{事件标题} {评价标签} {分数变化}」
        #   请以你的人格立场审视该现状，判断你介入处理此事的适配程度，给出参与度评分。
        #   评分规则：0-100 的整数，越高表示越该由你上场。
        #   输出格式（严格三行，不要多余内容）：
        #   评分：<0-100 的整数>
        #   理由：<一句话理由>
        #   行动：<一句话：若你上场，将采取的行动方向与思路>
        # 归因块构建失败直接上抛阻断（user_rules，关键状态缺失不吞错继续）。
        attribution = build_decision_feedback_prompt(get_feedback_state().snapshot())
        parts = ["当前现状：", self.current_summary or "（无）"]
        if attribution:
            parts += ["", attribution]
        parts += [
            "",
            "请以你的人格立场审视该现状，判断你介入处理此事的适配程度，给出参与度评分。",
            "评分规则：0-100 的整数，越高表示越该由你上场。",
            "输出格式（严格三行，不要多余内容）：",
            "评分：<0-100 的整数>",
            "理由：<一句话理由>",
            "行动：<一句话：若你上场，将采取的行动方向与思路>",
        ]
        query = "\n".join(parts)

        # 三段式骨架：前处理 → LLM 循环 → 后处理
        logger.info(f"人格自荐: {self.persona.persona_name}")
        processed_input = self._preprocess(query)
        loop_result = await self._llm_loop(processed_input)
        content = loop_result.get("content", "")
        extra_info = loop_result.get("extra_info")
        return self._postprocess(content, extra_info)
