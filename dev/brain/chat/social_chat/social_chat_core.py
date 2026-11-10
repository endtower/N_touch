# -*- coding: utf-8 -*-
"""social_chat 入口（Core）。

对外接口：
    async def start(
        self,
        purpose: str,
        working_session_id: str,
        persona_name: str = "",
    ) -> dict:

Returns:
    dict: 包含 summary_text / exit_reason。

persona_name（2026-09-29 起）：本段社交使用的人格名，由 PFCA social_chat_agent
工具入参显式指定（缺省 normal）；core 持有之，供 input_manager 在首条 user
消息注入【当前人格】段。

发送模式（审批 / 自由）由事件链权限服务统一持有，config 决定每条链的默认值。
同一链同一目标会话继承授权；升级需主人『同意』，本类不接收模式参数。
"""

import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from config.role_settings import RoleConfig

from brain.chat.social_chat.social_chat_input_manager import get_social_chat_input_manager
from brain.chat.social_chat.social_chat_session import SocialChatSession

logger = get_logger('SocialChatCore', "DEBUG")


class SocialChatCore:
    """social_chat 入口 / 状态机。

    调用流程：
        core = SocialChatCore()
        result = await core.start(
            purpose="...",
            working_session_id="group_xxx",
        )
    """

    def __init__(self):
        self._session: Optional[SocialChatSession] = None
        self._stop_requested = False
        self._transfer_summary = ""
        # 主人 / 工作会话 ID 统一在 start() 内被赋值；
        # 启动前 / 启动后任何未运行环节读取一律 fail-fast（不静默吞错）。
        self._master_session_id: Optional[str] = None
        self._working_session_id: Optional[str] = None
        # 本段社交使用的人格名（由 start() 注入，缺省 normal）；未初始化读一律 fail-fast。
        self._persona_name: Optional[str] = None
        self._start_time: Optional[float] = None
        # 汇报窗口起点（按会话分别快照 start() 时刻的最大 seq，纯序号、零时钟依赖）
        self._master_start_seq: Optional[int] = None
        self._working_start_seq: Optional[int] = None
        # 情感登记缓存（按 session_type 分别累计，由 emotional_judgment 工具写入）
        self._master_encounters: List[str] = []
        self._working_encounters: List[str] = []

    # ============================================================
    # ID 暴露（仅两个，single source of truth）
    # 任何模块读主人 / 工作会话 ID 只能走这里，禁止再从调用方或外部参数接住副本。
    # 启动前 / start() 未注入时直接崩溃阻断（符合 user_rules）。
    # ============================================================
    @property
    def master_session_id(self) -> str:
        """主人私聊会话 ID（private_<MASTER_ID>），由 RoleConfig 派生，start() 内赋值。"""
        if self._master_session_id is None:
            raise RuntimeError(
                "SocialChatCore.master_session_id 未初始化。"
                "请确认 core.start() 已执行，且主人 ID 可由 RoleConfig.MASTER_ID 派生。"
            )
        return self._master_session_id

    @property
    def working_session_id(self) -> str:
        """工作会话 ID（消息实际发出地），由调用方（PFCA social_chat_agent 工具）在 start() 时注入。"""
        if self._working_session_id is None:
            raise RuntimeError(
                "SocialChatCore.working_session_id 未初始化。"
                "请确认 social_chat_core.start() 已被调用并传入 working_session_id。"
            )
        return self._working_session_id

    @property
    def persona_name(self) -> str:
        """本段社交使用的人格名（由 PFCA social_chat_agent 工具在 start() 时注入，缺省 normal）。"""
        if self._persona_name is None:
            raise RuntimeError(
                "SocialChatCore.persona_name 未初始化。"
                "请确认 social_chat_core.start() 已被调用并传入 persona_name。"
            )
        return self._persona_name

    # ============================================================
    # 对外接口
    # ============================================================
    @async_handle_errors()
    async def start(
        self,
        purpose: str,
        working_session_id: str,
        persona_name: str = "",
    ) -> Dict[str, Any]:
        """启动一次 social_chat 委托。

        Args:
            purpose: 完整任务书（PFCA 的 task_info 直传，不再有前置商讨阶段）
            working_session_id: 工作会话 ID（消息实际发出地）
            persona_name: 本段社交使用的人格名（缺省 normal），
                由 PFCA social_chat_agent 工具入参透传。

        Returns:
            {
                "summary_text": str,          # 给 PFCA 的汇报（包含 master/working 两段）
                "exit_reason": str,
            }
        """
        if not purpose or not working_session_id:
            raise ValueError("start() 缺少必要参数")
        if not isinstance(working_session_id, str) or not working_session_id.strip():
            raise ValueError(f"start() 收到非法 working_session_id: {working_session_id!r}")

        # single source of truth：master_session_id 由 RoleConfig.MASTER_ID 派生，
        # working_session_id 由调用方传入。
        # 任何模块（input_manager / tool_manager / session / _build_result）
        # 都只能从这里读，禁止再从外部参数 / 自身属性缓存副本。
        self._master_session_id = f"private_{RoleConfig.MASTER_ID}"
        if not self._master_session_id.startswith("private_"):
            raise RuntimeError(
                f"SocialChatCore._master_session_id 派生异常: {self._master_session_id!r}"
            )
        self._working_session_id = working_session_id.strip()
        # 人格：工具入参显式指定，缺省 normal；由 input_manager 在首条消息注入内核。
        from brain.base.persona.persona_registry import get_default_persona_name
        self._persona_name = (persona_name or "").strip() or get_default_persona_name()
        self._stop_requested = False
        self._transfer_summary = ""
        self._start_time = time.time()
        # 重置情感登记缓存
        self._master_encounters = []
        self._working_encounters = []

        try:
            from brain.hc.global_session_state import get_global_session_state
            state = get_global_session_state()
            await state.add_pfca_session(self.master_session_id, "", search_impression=True)
            await state.add_pfca_session(self.working_session_id, "", search_impression=True)
        except Exception as e:
            logger.error(f"social_chat 注册 pfca_session 失败: {e}", exc_info=True)
            raise

        # 快照两个会话各自"启动那一刻"的最大 seq 作为汇报窗口起点（纯序号，零时钟依赖）。
        # 任一会话快照失败直接崩溃阻断，不允许静默回退。
        self._master_start_seq = await self._snapshot_latest_seq(self.master_session_id)
        self._working_start_seq = await self._snapshot_latest_seq(self.working_session_id)
        logger.info(
            f"social_chat 汇报窗口锚点: master_start_seq={self._master_start_seq}, "
            f"working_start_seq={self._working_start_seq}"
        )

        # 准备 input_manager（master / working 会话 ID 一律从 core 读，不再作为入参）
        input_manager = get_social_chat_input_manager()
        input_manager.reset()
        input_manager.prepare_first_message(
            purpose=purpose,
            social_chat_core=self,
        )

        # 启动 session（master / working 会话 ID 一律从 core 读，不再作为入参）
        session = SocialChatSession(social_chat_core=self)
        self._session = session
        try:
            await session.start()
        except Exception as e:
            logger.error(f"social_chat session 执行异常: {e}", exc_info=True)
            raise

        # 构造返回结果
        return await self._build_result(session)

    # ============================================================
    # 内部工具方法
    # ============================================================
    def set_transfer_summary(self, summary: str) -> None:
        self._transfer_summary = summary

    def add_emotion_encounter(self, session_type: str, act: str) -> None:
        """追加一条情感登记到指定 session_type 对应的缓存。

        Args:
            session_type: 'master' / 'working'，分别对应主人会话 / 工作会话。
            act: 遭遇/行为标签（与 ACT_EFFECTS 注册的 key 对齐）。
        """
        if not act or not act.strip():
            return
        if session_type == "master":
            self._master_encounters.append(act.strip())
        elif session_type == "working":
            self._working_encounters.append(act.strip())
        else:
            raise ValueError(
                f"add_emotion_encounter 收到非法 session_type: {session_type!r}"
                "（仅支持 'master' / 'working'）"
            )

    def get_emotion_encounters(self, session_type: str) -> List[str]:
        """读取指定 session_type 的情感登记列表（拷贝）。"""
        if session_type == "master":
            return list(self._master_encounters)
        if session_type == "working":
            return list(self._working_encounters)
        raise ValueError(
            f"get_emotion_encounters 收到非法 session_type: {session_type!r}"
            "（仅支持 'master' / 'working'）"
        )

    def is_stop_requested(self) -> bool:
        return self._stop_requested

    async def request_stop(self) -> None:
        self._stop_requested = True
        if self._session is not None:
            self._session.running = False

    async def _build_result(self, session: SocialChatSession) -> Dict[str, Any]:
        """构造 start() 的返回值。

        输出三段：
            1) 与主人的聊天记录：seq 起点 = start() 时 master 会话最大 seq
            2) 工作会话的聊天记录：seq 起点 = start() 时 working 会话最大 seq
            3) Chat Agent 补充说明（transfer_summary）
        """
        messages_sent = list(session.tool_manager._messages_sent)

        exit_reason = session._exit_reason or "transfer_back"
        if self._stop_requested and self._transfer_summary:
            exit_reason = "completed"

        # 各自快照锚点：start() 时刻两会话各自的最大 seq。
        # 锚点缺失 → fail-fast，避免拉全量历史污染汇报。
        if self._master_start_seq is None or self._working_start_seq is None:
            raise RuntimeError(
                "social_chat 汇报窗口锚点未初始化（master_start_seq / "
                "working_start_seq 必须由 start() 写入）。"
            )

        master_records = await self._fetch_phase_records(
            self.master_session_id, self._master_start_seq
        )
        working_records = await self._fetch_phase_records(
            self.working_session_id, self._working_start_seq
        )

        parts: List[str] = ["已通过 social_chat 处理本次对外发送任务。"]
        if master_records:
            parts.append(f"【与主人的聊天记录】\n{master_records}")
        if working_records:
            parts.append(f"【工作会话的聊天记录】\n{working_records}")

        chat_agent_summary = self._transfer_summary.strip() or "（无）"

        lines = parts + [
            "【Chat Agent 补充说明】",
            chat_agent_summary,
            f"实际发出 {len(messages_sent)} 条消息"
            + ("：" + "；".join(r["content"].replace("\n", " ")[:60] for r in messages_sent) if messages_sent else "。"),
        ]
        summary_text = "\n\n".join(lines)

        return {
            "summary_text": summary_text,
            "exit_reason": exit_reason,
        }

    @staticmethod
    async def _snapshot_latest_seq(session_id: str) -> int:
        """取会话当前最大 seq 作为窗口起点锚点（纯序号，零时钟依赖）。

        空会话返回 0；查询失败直接抛错阻断（符合 user_rules 不静默回退）。
        """
        from memory.message_repository import get_message_store

        store = get_message_store()
        latest = await store.get_latest_messages(session_id, 1)
        if not latest.get("messages"):
            return 0
        return int(latest["messages"][-1]["_seq"])

    async def _fetch_phase_records(self, session_id: str, start_seq: int) -> str:
        """抓取"start() 之后（seq > start_seq）"的会话消息，带时间戳，含 AI 自身发送。

        用消息库 seq 闭区间取数 + session_history.format_messages 渲染，
        不再做任何时间戳过滤。
        """
        if start_seq is None or start_seq < 0:
            raise RuntimeError(
                f"social_chat 阶段窗口起点非法：{session_id} start_seq={start_seq!r}"
            )

        from memory.message_repository import get_message_store
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        store = get_message_store()
        latest = await store.get_latest_messages(session_id, 1)
        latest_seq = int(latest["messages"][-1]["_seq"]) if latest.get("messages") else 0
        if latest_seq <= start_seq:
            return ""

        messages = await store.get_messages_by_seq_range(
            session_id, start_seq + 1, latest_seq
        )
        if not messages:
            return ""

        history = get_global_session_history()
        formatted_lines, _user_infos = history.format_messages(
            messages,
            FetchOptions(
                message_format=MessageFormatOptions(
                    show_new_marker=False,
                    show_timestamp=True,
                ),
            ),
        )
        return "\n".join(formatted_lines)


def get_social_chat_core() -> SocialChatCore:
    return SocialChatCore()
