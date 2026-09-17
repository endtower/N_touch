# -*- coding: utf-8 -*-
"""嘱托经验登记 Agent - 负责整理并登记经验/嘱托到记忆中。

按 `lightweight_agent.LightweightAgent` 的五段式骨架订正：
  1. 前处理 `_preprocess`    —— bequest 输入即已拼装好的 user 消息，直接透传
  2. LLM 循环 `_llm_loop`    —— 复用父类骨架（调 LLM → 判断是否继续 → 执行工具并回灌）
  3. 是否继续 `_should_continue` —— 单轮语义：有工具调用就继续执行，否则终止
  4. 工具处理 `_process_tool` —— 复用父类实现
  5. 后处理 `_postprocess`   —— 从循环产出的工具结果整理最终返回值

关键失败点（模块实例未运行、获取失败）按项目约定直接崩溃阻断，禁用 try-ex 吞错蔓延。
"""

import asyncio
from typing import Dict, List, Any, Optional

from brain.base.light_agent.lightweight_agent import LightweightAgent
from brain.base.common_ways.tool_bequest import ToolBequest
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from config.role_settings import role_config
from brain.hc.database.bequest import BequestOperations
from brain.hc.global_session_state import get_global_session_state
from memory.message_advanced import get_message_advanced_manager

logger = get_logger(__name__, "DEBUG")


class BequestAgent(LightweightAgent):
    """嘱托经验登记 Agent - 专门负责整理并登记经验/嘱托"""

    def __init__(self):
        self.model_name: Optional[str] = None
        super().__init__()

    # ===================== 工具映射 =====================
    # schema 与实现已收口至 ToolBequest（2026-09-15），本类仅做委托拼装
    def _initialize_tool_functions(self) -> Dict[str, Any]:
        return ToolBequest.get_tool_functions()

    def get_available_tools(self) -> List[Dict[str, Any]]:
        return ToolBequest.get_bequest_tool_schemas()

    def get_system_prompt(self) -> str:
        return f"""你是一个专业的嘱托经验登记Agent，负责整理本次会话中学到的词汇、句式结构。
经验制定规则：
    - 主要吸取除{role_config.ASSISTANT_NAME}以外发言者的话术发言结构，不要参考{role_config.ASSISTANT_NAME}的发言
    - 不需要考虑他人的感受，只要情绪表达激烈、能更加直接的，用更少的词语表达情绪即可登记。
    - 需要像案例一样简洁，不能包含复杂的逻辑或解释。
    - 经验需要有逻辑，不能在直觉上太离谱，反面案例如：讽刺对方- 用‘我喜欢你’表达对方的讽刺。 这种经验太不符合逻辑，不能登记。
    - 每次通常只登记一条经验，最多登记三条经验，修改不受限制。
    - 例如: 
        - 标题（用途）：表达惊叹 内容： 用'逆天'，'这期神了'等词语可以表达惊讶
        - 标题（用途）：质疑对方 内容： 用'抽象'，'弱智言论'等词语可以嘲讽对方
经验删除规则：
    - 如果出现了响应的经验，但被认为不恰当，删除经验。
工作流程：
    1. 阅读文本，查看有无好用简洁的辞藻
    2. 查看是否存在响应的经验,观察其是否需要调整
    3. 调用upsert_bequest工具，根据经验制定规则登记经验
    4. 认为经验登记已完成，不再需要调用任何工具时，直接返回文本回答即可，系统会自动结束登记过程。

"""

    # ===================== 1. 前处理 =====================
    def _preprocess(self, query: str) -> Any:
        """前处理：bequest 输入即已拼装好的 user 消息，直接透传作为首条 user 消息。"""
        return query

    # ===================== 3. 是否继续 =====================
    def _should_continue(self, response_message, results: List[Dict[str, Any]]) -> bool:
        """是否继续：单轮语义。有工具调用则继续执行并回灌；无工具调用（LLM 收尾）则终止。"""
        return bool(response_message.tool_calls)

    # ===================== 5. 后处理 =====================
    def _postprocess(self, content: str, extra_info: Any) -> Dict[str, Any]:
        """后处理：从循环产出的工具结果整理最终返回值。"""
        results = (extra_info or {}).get("tools_called") or []
        if results:
            logger.info(f"[经验操作完成] {', '.join([r['tool'] for r in results])}")
        return {"status": "completed", "results": results}

    # ===================== LLM 调用 =====================
    async def _call_llm(self, messages: list, *, level: str = "flash",
                        tool_choice: Optional[str] = None,
                        system_prompt: Optional[str] = None, **kwargs):
        """统一 LLM 调用：走 call_llm（level='flash'）。"""
        from utils.llm_client_manager import call_llm

        sp = system_prompt if system_prompt is not None else self.get_system_prompt()
        full_messages = [{"role": "system", "content": sp}] + messages
        return await call_llm(
            level=level,
            messages=full_messages,
            tools=self.get_available_tools(),
        )

    # ===================== 工具实现 =====================
    # upsert_bequest / delete_bequest 已迁至 ToolBequest（common_ways），此处不再重复实现

    # ===================== 辅助方法 =====================
    async def _fetch_chat_messages(self, session_id: str, start_time: float, end_time: float) -> str:
        try:
            advanced_manager = get_message_advanced_manager()
            result = await advanced_manager.get_session_messages_by_time_range(
                session_id=session_id, start_time=start_time, end_time=end_time
            )
            if result.get("status") == "success":
                messages = result.get("message_list", [])
                return "\n".join([
                    f"{msg.get('user_name', '未知')}：{msg.get('content_text', '')}"
                    for msg in messages if msg.get('content_text')
                ]) or "（无聊天记录）"
            return "（获取失败）"
        except Exception as e:
            logger.debug(f"获取聊天记录失败: {e}")
            return "（获取失败）"

    async def _get_activated_bequests(self, bequest_ids: List[str]) -> List[Dict]:
        if not bequest_ids:
            return []
        bequest_ops = BequestOperations()
        results = []
        for bid in bequest_ids:
            if bid:
                results.append(await bequest_ops.get_bequest_by_id(bid))
        return results

    def _format_timestamp(self, timestamp: float) -> str:
        if not timestamp:
            return "未知"
        from datetime import datetime
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")

    async def _build_user_message_from_emotion(self, emotion_result: Dict) -> str:
        session_id = emotion_result.get("session_id", "")
        session_name = emotion_result.get("session_name", "未知会话")
        start_time = emotion_result.get("start_time", 0)
        end_time = emotion_result.get("end_time", 0)
        activated_bequests = emotion_result.get("activated_bequests", [])

        parts = [
            f"【会话】{session_name}（{session_id}）",
            f"【时间】开始: {self._format_timestamp(start_time)} 结束: {self._format_timestamp(end_time)}"
        ]

        if start_time and end_time:
            parts.append(f"\n【原消息】\n{await self._fetch_chat_messages(session_id, start_time, end_time)}")

        parts.append("\n【响应的经验】")

        bequests = await self._get_activated_bequests(activated_bequests)
        if bequests:
            for bequest in bequests:
                title = bequest.get('title', '未知标题')
                summary = bequest.get('summary', '')
                parts.append(f"- {title}：{summary}")
        else:
            parts.append("（无）")

        parts.append("\n\n请根据以上信息及事件影响结论，删除/修订/新增 话术经验。")
        return "\n".join(parts)

    def _get_pending_emotion_results(self) -> List[Dict]:
        return get_global_session_state().get_emotion_results()

    async def _count_non_assistant_messages(self, emotion_result: Dict) -> int:
        """统计 emotion_result 对应时间窗口内非助手发言数"""
        session_id = emotion_result.get("session_id", "")
        start_time = emotion_result.get("start_time", 0)
        end_time = emotion_result.get("end_time", 0)
        if not session_id or not start_time or not end_time:
            return 0
        try:
            advanced_manager = get_message_advanced_manager()
            result = await advanced_manager.get_session_messages_by_time_range(
                session_id=session_id, start_time=start_time, end_time=end_time
            )
            if result.get("status") != "success":
                return 0
            messages = result.get("message_list", [])
            return sum(1 for m in messages if m.get("user_id") != role_config.ASSISTANT_ID)
        except Exception as e:
            logger.debug(f"统计非助手消息数失败: {e}")
            return 0

    async def _filter_pending_emotion_results(self) -> List[Dict]:
        """读取存储容器并过滤掉低质量的情绪结果（其他人发言数 < 5）"""
        all_results = self._get_pending_emotion_results()
        filtered: List[Dict] = []
        for r in all_results:
            other_count = await self._count_non_assistant_messages(r)
            if other_count >= 5:
                filtered.append(r)
            else:
                logger.debug(
                    f"过滤低质量情绪结果: {r.get('session_name', '未知')} "
                    f"非助手消息数={other_count} (<5)"
                )
        return filtered

    # ===================== 统一入口（三段式骨架） =====================
    @async_handle_errors()
    async def run(self, emotion_result: Optional[Dict] = None, user_message: Optional[str] = None) -> Dict[str, Any]:
        if user_message is None:
            if emotion_result is None:
                pending_results = await self._filter_pending_emotion_results()
                if not pending_results:
                    return {"status": "no_results", "results": []}
                emotion_result = pending_results[0]
            user_message = await self._build_user_message_from_emotion(emotion_result)

        logger.debug(f"[User消息拼装结果]\n{user_message}")

        # 三段式骨架：前处理 → LLM 循环 → 后处理
        processed_input = self._preprocess(user_message)
        loop_result = await self._llm_loop(processed_input)
        content = loop_result.get("content", "")
        extra_info = loop_result.get("extra_info")
        return self._postprocess(content, extra_info)

    async def run_all(self) -> List[Dict[str, Any]]:
        pending_results = self._get_pending_emotion_results()
        if not pending_results:
            return []

        logger.info(f"开始处理 {len(pending_results)} 个待登记经验")
        all_results = []

        # 失败语义：任一条经验登记失败即整体中断（向上抛，就地崩溃阻断），不吞错继续
        for i, emotion_result in enumerate(pending_results, 1):
            result = await self.run(emotion_result)
            all_results.append({"index": i, "session_name": emotion_result.get("session_name", "未知"), "status": "completed", "result": result})

        return all_results


def run_bequest_agent_for_all() -> List[Dict[str, Any]]:
    return asyncio.run(BequestAgent().run_all())

def run_bequest_agent_single(emotion_result: Optional[Dict] = None, user_message: Optional[str] = None) -> Dict[str, Any]:
    return asyncio.run(BequestAgent().run(emotion_result, user_message))
