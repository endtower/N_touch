"""
AtMentionTruck - 群聊中被@自己时的唤醒卡车

功能：
- 检测范围：群聊
- pfca 游标未读消息里含指向助手本人（role_settings.ASSISTANT_QQ）的 at 段即触发
- 命中后直接把该会话交给 station → PFCA（与 private_truck 同风格：命中即唤起，不经判断层）
- 触发后把 pfca 游标推进到会话最新消息，避免同一批消息重复触发

只在群里被点名才唤醒：没有被@的群聊即使有未读也不触发（助手不主动接所有群消息）。
"""

import time
from typing import List, Dict, Any

from utils.logger import get_logger
from .base_truck import BaseTruck

logger = get_logger(__name__, "INFO")

# 与 private_truck 一致：所有 truck 共用 pfca 游标做「未读」判定。
CONSUMER_ID = "pfca"
# 单次最多展示的未读消息数（数据层上限 MAX_UNREAD_FETCH_LIMIT = 50）
MAX_SHOW_UNREAD = 20
# 展示的总消息条数下限（未读不足时从历史补足，便于判断语境）
MIN_SHOW_MESSAGES = 10


def _format_duration(seconds: float) -> str:
    """把秒数格式化为时长描述（x年/x个月/x天/x小时/x分钟/x秒）。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时"
    days = hours // 24
    if days < 30:
        return f"{days}天"
    months = days // 30
    if months < 12:
        return f"{months}个月"
    return f"{months // 12}年"


class AtMentionTruck(BaseTruck):
    """群聊被@卡车 - 群里有消息@到助手本人即触发"""

    def __init__(self):
        super().__init__("at_mention_truck")

    # ==================== 检测 ====================

    @staticmethod
    def _self_qq() -> str:
        """助手本人 QQ（群聊消息的 at 段 data.qq 命中该值即视为 @ 到助手）。"""
        from config.role_settings import role_config
        return str(role_config.ASSISTANT_QQ).strip()

    @classmethod
    def _is_at_self(cls, msg: Dict[str, Any]) -> bool:
        """判断某条群消息是否 @ 了助手本人（检查 content 里的 at 段）。"""
        self_qq = cls._self_qq()
        if not self_qq:
            return False
        content = msg.get("content") or []
        if not isinstance(content, list):
            return False
        for seg in content:
            if not isinstance(seg, dict) or seg.get("type") != "at":
                continue
            data = seg.get("data") or {}
            target = str(data.get("qq") or data.get("user_id") or "").strip()
            if target == self_qq:
                return True
        return False

    async def _find_triggered_sessions(self) -> List[Dict[str, Any]]:
        """扫描有未读的群聊，返回其中 @ 到助手本人的会话。

        判定基于 pfca 游标的「未读」消息：只要未读里有任一条 @ 到助手，就算一次触发
        （不会把历史消息反复当成新触发）。

        Returns:
            List[Dict]: 按@消息的 _seq 倒序，每项含会话信息、未读消息、@ 到助手的消息。
        """
        from memory.message_advanced import get_message_advanced_manager
        from memory.message_repository import get_message_store

        message_advanced_manager = get_message_advanced_manager()
        store = get_message_store()

        unread_sessions = await message_advanced_manager.get_unread_sessions_by_type("group")

        triggered = []
        for session in unread_sessions:
            session_id = session.get("session_id", "")
            if not session_id:
                continue

            unread_messages = await store.get_unread_for(
                session_id, CONSUMER_ID, limit=MAX_SHOW_UNREAD, newest=True
            )
            mention_messages = [m for m in unread_messages if self._is_at_self(m)]
            if not mention_messages:
                continue

            triggered.append({
                "session_id": session_id,
                "session_type": "group",
                "session_name": session.get("session_name", ""),
                "remark": session.get("remark", ""),
                "last_update_time": session.get("last_update_time", 0),
                "unread_count": session.get("unread_count", 0),
                "unread_messages": unread_messages,
                "mention_messages": mention_messages,
            })

        triggered.sort(
            key=lambda s: s["mention_messages"][-1].get("_seq", 0),
            reverse=True,
        )
        return triggered

    async def _main_loop(self):
        """主循环 - 每 CYCLE_TIME 秒检测一次群里有没有@到助手本人的新消息"""
        logger.info("AtMentionTruck 已启动")
        while self.state != "stopped":
            triggered = await self._find_triggered_sessions()
            if triggered:
                self.logger.info(
                    f"群聊中有 {len(triggered)} 个会话@到助手本人，唤醒 station"
                )
                await self.remind_station()
            else:
                self.logger.debug("未检测到@助手本人的群聊新消息")
            await self.time_control()

    async def time_control(self):
        """时间控制 - 控制truck的运行时间，支持暂停功能"""
        from config.truck_settings import at_mention_truck_config
        total_sleep_time = at_mention_truck_config.CYCLE_TIME
        await self.cycle_control(total_sleep_time)

    # ==================== 取消息 ====================

    async def get_at_mention_messages(self):
        """取触发会话：@ 到助手的群聊 + 最近消息列表。

        未读不足 MIN_SHOW_MESSAGES 时从游标之前补历史，便于判断语境。
        """
        triggered = await self._find_triggered_sessions()
        if not triggered:
            self.logger.debug("未获取到@助手本人的群聊新消息")
            return None

        from memory.message_repository import get_message_store

        store = get_message_store()
        session = triggered[0]
        session_id = session["session_id"]
        unread_messages = session.get("unread_messages", [])

        filler_messages = []
        if len(unread_messages) < MIN_SHOW_MESSAGES:
            filler_messages = await store.get_messages_before_cursor(
                session_id, CONSUMER_ID, limit=MIN_SHOW_MESSAGES - len(unread_messages)
            )

        session["message_list"] = filler_messages + unread_messages
        self.logger.info(f"获取到@助手本人的群聊会话: {session_id}")
        return session

    # ==================== 格式化 ====================

    async def format_at_mention_messages(self, message):
        """格式化触发会话：写明被@ + 最近消息 + 参与提示。

        展示完成后推进 pfca 游标到会话最新消息（与 private_truck 一致），
        保证同一批消息不会在下一轮重复触发。
        """
        if not message:
            return "暂无@助手本人的群聊消息"

        from memory.message_repository import get_message_store
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        store = get_message_store()
        session_id = message.get("session_id", "")
        session_name = message.get("session_name", "") or session_id

        messages = message.get("message_list", [])
        total_unread = await store.count_unread_for(session_id, CONSUMER_ID)
        mention_messages = message.get("mention_messages", [])

        # @ 消息的发送者（可能多人）：@ 段之前的最后一条用户消息即发言人
        senders = []
        for m in mention_messages:
            name = m.get("user_name") or m.get("user_id") or "某人"
            if name not in senders:
                senders.append(name)
        sender_text = "、".join(senders) if senders else "某人"

        result_lines = [
            f"群聊「{session_name}」（{session_id}）中，{sender_text} @ 了你。"
        ]
        if mention_messages:
            # 积压时长：取最新一条@消息的落库时间距现在的时长
            latest_ts = max((m.get("timestamp", 0) for m in mention_messages), default=0)
            if latest_ts > 0:
                backlog_seconds = time.time() - latest_ts
                if backlog_seconds > 3600:
                    result_lines[0] = (
                        f"群聊「{session_name}」（{session_id}）中，{sender_text} @ 了你，"
                        f"已积压{_format_duration(backlog_seconds)}（并非刚刚收到），"
                        f"请先评估其语境是否已过时；回复时注意交代时序。"
                    )
        if total_unread > len(messages):
            result_lines.append(
                f"该会话共 {total_unread} 条未读消息，仅显示最近 {len(messages)} 条。"
            )

        result_lines.append("[该会话最近消息]")
        history = get_global_session_history()
        show_lines, _ = history.format_messages(
            messages,
            FetchOptions(
                message_format=MessageFormatOptions(
                    show_new_marker=True,
                    show_timestamp=True,
                )
            ),
        )
        result_lines.extend(show_lines)
        result_lines.append("有人在群里点名找你，可选择回应或观察。")

        # 触发后把 pfca 游标推进到会话最新消息（只进不退），避免重复触发
        current_cursor = await store.get_cursor(session_id, CONSUMER_ID)
        latest = await store.get_latest_session_messages(session_id, 1)
        latest_messages = latest.get("message_list", [])
        if latest_messages:
            latest_seq = latest_messages[0]["_seq"]
            if latest_seq > current_cursor:
                await store.advance_cursor(session_id, CONSUMER_ID, latest_seq)
                logger.info(
                    f"AtMentionTruck 触发后推进 pfca 游标 "
                    f"{current_cursor} -> {latest_seq} ({session_id})"
                )

        return "\n".join(result_lines)

    # 使用基类的remind_station方法

    async def get_truck_messages(self):
        """获取truck的消息，包括格式化后的消息和原始会话数据

        Returns:
            Dict: 包含格式化消息和原始会话数据的字典
                - formatted_message: str 格式化后的消息
                - raw_sessions: Dict 原始会话数据
            None: 如果没有需要处理的消息
        """
        unread_data = await self.get_at_mention_messages()

        # 如果没有获得unread_data，则直接返回None
        if not unread_data:
            return None

        formatted_message = await self.format_at_mention_messages(unread_data)

        return {
            "formatted_message": formatted_message,
            "raw_sessions": unread_data,
        }
