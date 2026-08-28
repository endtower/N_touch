# -*- coding: utf-8 -*-
import re
import time
from typing import List, Optional, Dict, Any
from datetime import datetime
from utils.logger import get_logger
from utils.error_handling import async_handle_errors
from brain.base.heavy_agent.heavy_core import HeavyCore

logger = get_logger('ConversationCore', "INFO")

# 匹配对话文本中的附件占位符：[图片:xxx] / [文件:xxx] / [表情:xxx] / [语音:xxx] / [视频:xxx]
_ATTACHMENT_PATTERN = re.compile(r'\[(图片|表情|语音|视频|文件):([^\]]+)\]')


def _extract_attachment_refs(text: str) -> List[str]:
    """从文本中按出现顺序提取去重后的附件引用（如 [图片:web_xxx.png]）"""
    seen = set()
    refs: List[str] = []
    for match in _ATTACHMENT_PATTERN.finditer(text or ""):
        ref = match.group(0)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


def get_conversation_core() -> 'ConversationCore':
    """获取 ConversationCore 单例实例"""
    return ConversationCore()


class ConversationCore(HeavyCore):
    """conversation 核心：聚焦会话的前处理与后处理。

    职责划分（继承自 HeavyCore 的统一生命周期骨架）：
    - 统一入口：start / stop（继承自 HeavyCore，子类不重写）
    - 兼容入口：start_session / stop_session（对外暴露，委托统一骨架）
    - 前处理：_preprocess（会话启动前的准备）
    - 后处理：_postprocess（会话结束后的收尾）
    """

    _instance: "Optional[ConversationCore]" = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        super().__init__()
        self._session = None
        # 会话停止时由 stop_session 记录、后处理 _postprocess 消费的退出信息
        self._pending_exit_reason: Optional[str] = None
        self._pending_progress_summary: str = ""
        self._encounters_cache: List[str] = []
        self._emotion_type_cache: str = ""
        self._pfca_strategy_cache: str = ""
        # 本次会话中由 LLM 通过 recognize_image 工具识别并保存的图片记录
        self._recognized_images: List[Dict[str, Any]] = []
        self._initialized = True
        logger.debug("初始化 ConversationCore")

    # ============================================================
    # 兼容入口（对外暴露，委托统一骨架）
    # ============================================================

    @async_handle_errors()
    async def start_session(
        self,
        session_id: str,
        purpose: str,
        max_messages: int = 0,
    ) -> str:
        """启动一次新会话（兼容入口）：委托给统一骨架 start()。

        流程：前处理 _preprocess() → 创建并运行重型 Session → 后处理 _postprocess()。

        Args:
            session_id: 会话标识，格式为 "type_id"，如 "private_1234567890" / "group_198431299"
            purpose: 本次会话的目的（已包含任何必要的补充信息；上游自行拼装）
            max_messages: 软限制消息条数
        """
        return await self.start(
            session_id=session_id,
            purpose=purpose,
            max_messages=max_messages,
        )

    @async_handle_errors()
    async def stop_session(self, exit_reason: str, progress_summary: str = "") -> None:
        """停止会话（兼容入口）：记录退出信息并请求停止 Session。

        后处理 _postprocess() 由骨架 start() 在 Session 结束后统一执行，
        这里只记录退出信息（供后处理消费）并停止 Session 循环。
        """
        self._pending_exit_reason = exit_reason
        self._pending_progress_summary = progress_summary
        await self.stop(exit_reason)

    # ============================================================
    # 前处理（抽象实现）
    # ============================================================

    async def _preprocess(
        self,
        session_id: str,
        purpose: str,
        **kwargs,
    ) -> None:
        """会话启动前的前处理：重置状态、初始化上下文。

        注：附件清单已并入 purpose（PFCA 在上游拼装时直接写进目的描述里），
        不再单独传 extra_file。
        """
        from brain.chat.conversation.conversation_input_manager import get_conversation_input_manager

        # 上一轮会话已由骨架 start() 统一收尾（self._session 已清空），这里直接重置本轮状态
        self._pending_exit_reason = None
        self._pending_progress_summary = ""
        self._clear_session_cache()
        get_conversation_input_manager().reset()
        logger.debug(f"启动会话，session_id={session_id}, purpose={purpose}")

        self.model_name = 'conversation'
        self.conversation_id = session_id
        # 本次会话目的由 PFCA 走 tool 链路直接传入，conversation 自身持有
        self.purpose = purpose

        self._get_session_info_from_focus()
        self._init_session_state()

    # ============================================================
    # 创建 Session（抽象实现）
    # ============================================================

    async def _create_session(self, max_messages: int = 0, **kwargs):
        """创建并返回本次会话的 ConversationSession 实例。"""
        from brain.chat.conversation.conversation_session import ConversationSession
        return ConversationSession(self, max_messages=max_messages)

    # ============================================================
    # 后处理（抽象实现）
    # ============================================================

    async def _postprocess(self, session, result, **kwargs) -> str:
        """会话结束后的后处理：拉取历史、拼装退出消息、持久化并清理，返回最终结果。"""
        from memory.session_history import get_global_session_history, FetchOptions, MessageFormatOptions

        # 退出信息由 stop_session 在会话期间记录，这里统一消费
        exit_reason = self._pending_exit_reason or "会话结束"
        progress_summary = self._pending_progress_summary or ""

        history = get_global_session_history()
        chat_content = ""
        actual_exit_reason = exit_reason

        if self.conversation_id and self.start_time:
            # 最终返回给 PFCA：拉取该会话中 pfca 游标之后（上次处理后）的全部消息，
            # 并推进 pfca 游标。这样 PFCA 无论如何都会收到该会话从上次到现在的完整消息，
            # 不再依赖 conversation 自己维护的"起始/结束游标"。
            fetch_result = await history.fetch_unread(
                self.conversation_id,
                getattr(self, "session_name", "") or "",
                consumer_id="pfca",
                options=FetchOptions(
                    message_format=MessageFormatOptions(
                        show_new_marker=False,
                        show_timestamp=False
                    )
                ),
                advance_cursor=True,
                min_messages=0,
            )

            chat_content = "\n".join(fetch_result.formatted_lines) if fetch_result.formatted_lines else ""

            if not chat_content:
                format_message = f"阶段性对话结束，原因：{exit_reason}，总结：{progress_summary}没有产生消息"
            else:
                format_message = f"阶段性对话结束，原因：{exit_reason}，总结：{progress_summary}\n{chat_content}"

            # 把"会话中由 LLM 识图工具识别并保存的图片"在末尾单独成行提示 PFCA
            # （只展示 LLM 主动调用 recognize_image 识别过的图，不再罗列所有附件）
            if self._recognized_images:
                lines = ["本段对话中 LLM 通过识图工具识别并保存的图片："]
                for idx, img in enumerate(self._recognized_images, 1):
                    saved_filename = img.get("saved_filename", "")
                    description = img.get("description", "")
                    mood = img.get("mood", "")
                    # 用 [图片:xxx] 格式引用，和发送图片一致
                    lines.append(
                        f"{idx}. [图片:{saved_filename}]\n"
                        f"   描述：{description}\n"
                        f"   感觉：{mood}"
                    )
                format_message += "\n\n" + "\n".join(lines)

            actual_exit_reason = format_message
            session._exit_reason = format_message

        # 确保 Session 已停止（max_rounds 自然退出时 running 仍为 True）
        await session.stop()

        await self._persist_and_cleanup(actual_exit_reason)

        # format_message 仅在 conversation_id/start_time 都存在时才会被赋值，
        # 这里用 actual_exit_reason 兜底打印，避免 UnboundLocalError。
        logger.debug(actual_exit_reason or f"会话结束，原因：{exit_reason}")
        return actual_exit_reason

    # ============================================================
    # 会话内辅助方法
    # ============================================================

    def add_recognized_image(
        self,
        saved_filename: str,
        title: str,
        description: str,
        mood: str,
    ) -> None:
        """
        追加一条"会话中由 LLM 识图工具识别并保存的图片"记录。
        会话结束时由 stop_session() 把这些记录拼进 format_message 末尾。
        """
        self._recognized_images.append({
            "saved_filename": saved_filename,
            "title": title,
            "description": description,
            "mood": mood,
        })
        logger.debug(f"追加识图记录: {saved_filename} ({title})")

    def has_emotion_evaluation(self) -> bool:
        """检查是否存在情绪评价"""
        return bool(self._encounters_cache) or bool(self._emotion_type_cache)
    
    def set_emotion_evaluation(self, emotion_type: str = "", encounters: List[str] = None, pfca_strategy: str = ""):
        """设置情绪评价信息
        
        Args:
            emotion_type: 情绪类型
            encounters: 遭遇情况列表
            pfca_strategy: PFCA策略指令
        """
        if emotion_type:
            self._emotion_type_cache = emotion_type
        if encounters:
            self._encounters_cache = encounters
        if pfca_strategy:
            self._pfca_strategy_cache = pfca_strategy
        logger.debug(f"已缓存情绪评价: emotion={emotion_type}, encounters={encounters}, pfca_strategy={'已设置' if pfca_strategy else '未设置'}")

    async def _persist_and_cleanup(self, exit_reason: str):
        """持久化情绪数据并清理缓存"""
        from brain.chat.conversation.conversation_bequest import get_conversation_bequest
        from brain.hc.database.bequest import BequestOperations
        from brain.hc.global_session_state import get_global_session_state
        from config.settings import chat_config
        
        focus_state = get_global_session_state()
        bequest_cache = get_conversation_bequest()
        
        conversation_key = self.conversation_id or ""

        activated = bequest_cache.get_all()
        fixed_title = getattr(chat_config, 'FIXED_BEQUEST_TITLE', None)
        if fixed_title:
            bequest_ops = BequestOperations()
            fixed_id = await bequest_ops.get_bequest_id_by_title(fixed_title)
            if fixed_id and fixed_id not in activated:
                activated.append(fixed_id)

        if self.has_emotion_evaluation() and conversation_key:
            focus_state.add_emotion_result(
                conversation_key=conversation_key,
                session_id=self.conversation_id,
                session_name=self.session_name,
                start_time=self.start_time,
                end_time=datetime.now().timestamp(),
                emotion_type=self._emotion_type_cache,
                pfca_strategy=self._pfca_strategy_cache,
                exit_reason=exit_reason,
                encounters=self._encounters_cache,
                activated_bequests=activated
            )
            logger.info(f"情绪评价已持久化，activated_bequests: {len(activated)} 条（含固定经验）")
        else:
            logger.debug("无情绪评价，跳过持久化")
        
        self._clear_session_cache()
        bequest_cache.clear()
        await self._cleanup_on_stop()
    
    def _clear_session_cache(self):
        """清空本次会话的临时缓存"""
        self._encounters_cache = []
        self._emotion_type_cache = ""
        self._pfca_strategy_cache = ""
        self._recognized_images = []
        logger.debug("会话缓存已清空")

    # ============================================================
    # 会话内辅助方法（conversation 特有，原 CoreBase 提供）
    # ============================================================

    def _get_session_info_from_focus(self) -> None:
        """从全局会话状态读取当前会话的名称（conversation 特有）。"""
        from brain.hc.global_session_state import get_global_session_state
        focus_state = get_global_session_state()
        session = focus_state.get_session(self.conversation_id)
        self.session_name = session.get("session_name", "") if session else ""

    def _init_session_state(self) -> None:
        """初始化会话状态（conversation 特有）。"""
        self.start_time = time.time()

    @async_handle_errors()
    async def _cleanup_on_stop(self) -> None:
        """停止时的清理工作（conversation 特有）。"""
        self.start_time = None
        from brain.chat.chat_history_manager import remove_history_manager
        remove_history_manager(prefix=self.model_name)
        logger.debug(f"已清理历史管理器实例，prefix: {self.model_name}")
