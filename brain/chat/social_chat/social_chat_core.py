# -*- coding: utf-8 -*-
"""social_chat 入口（Core）。

对外接口：
    async def start(
        self,
        purpose: str,
        working_session_id: str,
        preset_messages: list,
        enforcement_level: str = 'L1',
    ) -> dict:

Returns:
    dict: 包含 summary_text / sent_indices / skipped_indices / exit_reason。
"""

import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from config.role_settings import RoleConfig
from config.settings import chat_config

from brain.chat.social_chat.social_chat_input_manager import get_social_chat_input_manager
from brain.chat.social_chat.social_chat_session import SocialChatSession

logger = get_logger('SocialChatCore', "DEBUG")

# 系统硬编码的限制等级（统一从 chat_config 读取，详见 config.settings.ChatConfig.SOCIAL_CHAT_ENFORCEMENT_LEVEL）
DEFAULT_ENFORCEMENT_LEVEL = chat_config.SOCIAL_CHAT_ENFORCEMENT_LEVEL


class SocialChatCore:
    """social_chat 入口 / 状态机。

    调用流程：
        core = SocialChatCore()
        result = await core.start(
            purpose="...",
            working_session_id="group_xxx",
            preset_messages=[{"text":"..."}, ...],
            enforcement_level="L3",
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
        self._preset_messages: List[dict] = []
        self._enforcement_level: str = DEFAULT_ENFORCEMENT_LEVEL
        self._start_time: Optional[float] = None
        # 阶段汇报窗口起点（按会话分别快照，由 start() 在 add_pfca_session 之后写入）
        self._master_window_start: Optional[float] = None
        self._working_window_start: Optional[float] = None
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
        """工作会话 ID（消息实际发出地），由 social_agent 在 start() 时注入。"""
        if self._working_session_id is None:
            raise RuntimeError(
                "SocialChatCore.working_session_id 未初始化。"
                "请确认 social_chat_core.start() 已被调用并传入 working_session_id。"
            )
        return self._working_session_id

    # ============================================================
    # 对外接口
    # ============================================================
    @async_handle_errors()
    async def start(
        self,
        purpose: str,
        working_session_id: str,
        preset_messages: list,
        enforcement_level: str = DEFAULT_ENFORCEMENT_LEVEL,
    ) -> Dict[str, Any]:
        """启动一次 social_chat 委托。

        Args:
            purpose: 完整任务书 + 当前所处阶段标注
            working_session_id: 工作会话 ID（消息实际发出地）
            preset_messages: 预设条目池（social_agent 已排好顺序）
            enforcement_level: 限制等级 L1 / L2 / L3（默认 L1）

        Returns:
            {
                "summary_text": str,        # 给 social_agent 的汇报（包含 master/working 两段）
                "sent_indices": list[int],
                "skipped_indices": list[int],
                "exit_reason": str,
            }
        """
        if not purpose or not working_session_id or not preset_messages:
            raise ValueError("start() 缺少必要参数")
        if not isinstance(working_session_id, str) or not working_session_id.strip():
            raise ValueError(f"start() 收到非法 working_session_id: {working_session_id!r}")

        # single source of truth：master_session_id 由 RoleConfig.MASTER_ID 派生，
        # working_session_id 由 social_agent 调用方传入。
        # 任何模块（input_manager / tool_manager / session / _build_result）
        # 都只能从这里读，禁止再从外部参数 / 自身属性缓存副本。
        self._master_session_id = f"private_{RoleConfig.MASTER_ID}"
        if not self._master_session_id.startswith("private_"):
            raise RuntimeError(
                f"SocialChatCore._master_session_id 派生异常: {self._master_session_id!r}"
            )
        self._working_session_id = working_session_id.strip()
        self._preset_messages = list(preset_messages)
        self._enforcement_level = (enforcement_level or DEFAULT_ENFORCEMENT_LEVEL).upper()
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

        # 快照两个会话各自"启动那一刻"的 last_processed_time 作为汇报窗口起点。
        # 若任一会话缺失 last_processed_time（会话未注册 / 状态异常），按 user_rules
        # 直接崩溃阻断，不允许静默回退到 0 拉全量历史。
        self._master_window_start = self._read_pfca_processed_time(
            self.master_session_id
        )
        self._working_window_start = self._read_pfca_processed_time(
            self.working_session_id
        )
        logger.info(
            f"social_chat 汇报窗口锚点: master_start={self._master_window_start:.3f}, "
            f"working_start={self._working_window_start:.3f}"
        )

        # 准备 input_manager（master / working 会话 ID 一律从 core 读，不再作为入参）
        input_manager = get_social_chat_input_manager()
        input_manager.reset()
        input_manager.prepare_first_message(
            purpose=purpose,
            preset_messages=preset_messages,
            enforcement_level=enforcement_level,
            social_chat_core=self,
        )

        # 启动 session（master / working 会话 ID 一律从 core 读，不再作为入参）
        session = SocialChatSession(
            social_chat_core=self,
            preset_messages=self._preset_messages,
            enforcement_level=self._enforcement_level,
        )
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
        """构造 start() 的返回值（按设计文档 §6）。

        输出三段：
            1) 与主人的聊天记录：起点 = start() 时 master 的 last_processed_time
            2) 工作会话的聊天记录：起点 = start() 时 working 的 last_processed_time
            3) Chat Agent 补充说明（含 transfer_summary 与发送统计）
        """
        sent_indices = sorted(list(session.tool_manager._sent_indices))
        skipped_indices = sorted(set(range(1, len(self._preset_messages) + 1)) - set(sent_indices))

        exit_reason = session._exit_reason or "transfer_to_social"
        if self._stop_requested and self._transfer_summary:
            exit_reason = "completed"

        # 各自快照锚点：start() 时刻两会话各自的 last_processed_time。
        # 锚点缺失 → fail-fast，避免拉全量历史污染汇报。
        if self._master_window_start is None or self._working_window_start is None:
            raise RuntimeError(
                "social_chat 汇报窗口锚点未初始化（master_window_start / "
                "working_window_start 必须由 start() 写入）。"
            )

        master_records = await self._fetch_master_records(self._master_window_start)
        working_records = await self._fetch_working_records(self._working_window_start)

        parts: List[str] = ["已通过 social_chat 处理本次对外发送任务。"]
        if master_records:
            parts.append(f"【与主人的聊天记录】\n{master_records}")
        if working_records:
            parts.append(f"【工作会话的聊天记录】\n{working_records}")

        chat_agent_summary = self._transfer_summary.strip() or "（无）"

        lines = parts + [
            "【Chat Agent 补充说明】",
            chat_agent_summary,
            f"已发 {len(sent_indices)} 条（条目 {sent_indices}），"
            f"未发 {len(skipped_indices)} 条（条目 {skipped_indices}）。",
        ]
        summary_text = "\n\n".join(lines)

        return {
            "summary_text": summary_text,
            "sent_indices": sent_indices,
            "skipped_indices": skipped_indices,
            "exit_reason": exit_reason,
        }

    def _read_pfca_processed_time(self, session_id: str) -> float:
        """从会话数据里读指定会话当前的 end_time（兼容旧 last_processed_time）。

        缺失则 fail-fast（按 user_rules 不允许静默回退），由调用方在
        start() 阶段持有异常决定是否阻断。
        """
        from brain.hc.global_session_state import get_global_session_state

        state = get_global_session_state()
        session = state.get_session(session_id)
        if session is None:
            raise RuntimeError(
                f"social_chat 锚点缺失：{session_id} 不在会话数据内。"
                "请确认 social_chat_core.start() 已通过 add_pfca_session 注册。"
            )
        ts = session.get("end_time")
        if ts is None or ts <= 0:
            raise RuntimeError(
                f"social_chat 锚点缺失：{session_id} 的 end_time "
                "未记录或为 0。会话注册异常，按 user_rules 直接阻断。"
            )
        return float(ts)

    async def _fetch_master_records(self, report_window_start: float) -> str:
        """主人会话：阶段窗口（report_window_start → now）的全部消息，带时间戳，无已读状态。"""
        return await self._fetch_phase_records(
            self._master_session_id,
            start_time=report_window_start,
        )

    async def _fetch_working_records(self, report_window_start: float) -> str:
        """工作会话：阶段窗口（report_window_start → now）的全部消息，带时间戳，无已读状态。"""
        return await self._fetch_phase_records(
            self._working_session_id,
            start_time=report_window_start,
        )

    async def _fetch_phase_records(
        self,
        session_id: str,
        start_time: float,
    ) -> str:
        """抓取"阶段起点 → now"的会话消息，带时间戳，无已读状态。"""
        if not session_id:
            return ""
        if start_time is None or start_time <= 0:
            raise RuntimeError(
                f"social_chat 阶段窗口起点非法：{session_id} start_time={start_time!r}"
            )

        from brain.hc.global_session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        history = get_global_session_history()
        try:
            result = await history.fetch(
                session_id, "",
                start_time=float(start_time),
                end_time=datetime.now().timestamp(),
                options=FetchOptions(
                    message_format=MessageFormatOptions(
                        show_read_status=False,
                        show_timestamp=True,
                    ),
                ),
            )
        except Exception as e:
            logger.warning(f"social_chat 抓取 {session_id} 失败: {e}")
            return ""

        if not result or not getattr(result, "formatted_lines", None):
            return ""
        return "\n".join(result.formatted_lines)


def get_social_chat_core() -> SocialChatCore:
    return SocialChatCore()