# -*- coding: utf-8 -*-

"""
通讯录模块，用于管理和查询联系人信息

会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。
通讯录内部仍按类型分桶（private / group），但通过 session_id 前缀过滤。

纯异步实现：所有数据加载、查询均为 async
"""

from paths import data_root
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime
from utils.logger import setup_logging
from adapt.session_identity import session_scope

logger = setup_logging(__name__, log_level="INFO")


def _extract_session_type(session_id: str) -> str:
    """从统一 session_id 中提取 session_type（type_id 格式的第一段）"""
    if not session_id or "_" not in session_id:
        return ""
    return session_scope(session_id)


class AddressBook:
    """通讯录类，用于管理和查询联系人信息（纯异步）

    刷新策略（2026-10-25）：通讯录不再一次性缓存到进程退出。每次读取都按会话表
    重新聚合（一条 SQL 拿全部会话的消息数 / 最近活跃数 / 最后一条消息），
    因此新会话与刚收到的回复会立刻出现在通讯录里，不需要重启 Core。
    只有「第一印象」描述按 session_id 缓存——它涉及节点创建，且名称不变时无需重查。
    """

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._storage_path = data_root() / "memory" / "pfca" / "address_book"
            self._storage_path.mkdir(parents=True, exist_ok=True)
            self._contacts = {
                "private": [],
                "group": []
            }
            # session_id -> (session_name, description)：名称变化时重查印象
            self._descriptions: Dict[str, tuple] = {}
            self._initialize_lock = None  # 在第一次异步调用时建

    def _build_contact(self, session_id: str, session_name: str,
                       last_update_time: float, message_count: int,
                       recent_count: int, last_user_name: str,
                       last_content_text: str, last_timestamp: float,
                       session_type: str, description: str,
                       conversation_allowed: bool = False) -> Dict[str, Any]:
        """从一条聚合结果构建联系人信息字典（纯字符串处理）

        状态判定：当前最近 10 分钟内消息数 >= 10 → 活跃，否则不活跃。

        conversation_allowed：该会话是否可直接用 conversation 模式进入（无需主人参与）。
        由 _refresh_contacts 传入（保护模式放行集合判定结果），此处仅原样落字段。
        """
        last_message = ""
        if last_timestamp:
            time_str = datetime.fromtimestamp(last_timestamp).strftime("%m月%d号 %H:%M:%S")
            last_message = f"[{time_str}] {last_user_name}:{last_content_text}"

        return {
            "name": session_name,
            "description": description,
            "contact_info": {
                "session_id": session_id
            },
            "last_processed_time": datetime.fromtimestamp(last_update_time).strftime("%Y-%m-%d %H:%M:%S"),
            "status": "active" if recent_count >= 10 else "inactive",
            "recent_10min_count": recent_count,
            "message_count": message_count,
            "last_message": last_message,
            "conversation_allowed": conversation_allowed,
            "_session_type": session_type
        }

    async def initialize(self):
        """异步初始化：确保锁存在（真正的数据刷新在每次读取时进行）。"""
        if self._initialize_lock is None:
            import asyncio
            self._initialize_lock = asyncio.Lock()

    async def _describe(self, session_id: str, session_name: str, session_type: str) -> str:
        """取该会话的第一印象描述；名称未变时复用缓存，避免重复查库与建节点。"""
        cached = self._descriptions.get(session_id)
        if cached is not None and cached[0] == session_name:
            return cached[1]

        from brain.hc.database.character_compose import CharacterCompose
        try:
            impression_result = await CharacterCompose().first_session_impression_search(
                name=session_name,
                session_id=session_id
            )
            if session_type == "private":
                description = impression_result.get(
                    "formatted_info", f"你对{session_name}没有任何印象。"
                )
            else:
                description = impression_result.get(
                    "formatted_info", f"群组{session_name}。"
                )
        except Exception as e:
            # 单个联系人的印象查询失败不阻断整本通讯录（降级为空印象）。
            logger.error(f"获取第一印象失败: {e}")
            if session_type == "private":
                description = f"你对{session_name}没有任何印象。"
            else:
                description = f"群组{session_name}。"

        self._descriptions[session_id] = (session_name, description)
        return description

    async def _refresh_contacts(self):
        """按会话表现况重建通讯录（每次读取都执行）。

        加载失败直接抛异常阻断（硬约定 #4），不吞异常伪装"空通讯录"。
        一条聚合 SQL 取代原先每个会话一次的全表消息读取，刷新成本与会话数线性但常数极小。
        """
        import time
        from brain.hc.database.core import get_memory_db
        from config.settings import SafetyConfig
        from config.role_settings import RoleConfig
        from memory.message_repository import get_message_store

        message_store = get_message_store()

        # 保护模式放行集合（三源，与 pfca_tools.conversation_agent 的判定口径一致）：
        # 主人私聊 + 白名单 + 记忆里勾选「允许 conversation」的实体会话，一次性取好。
        protection_on = SafetyConfig.CONVERSATION_PROTECTION_MODE
        master_private = f"private_{RoleConfig.MASTER_ID}"
        allowed_sessions = (
            {master_private}
            | set(SafetyConfig.CONVERSATION_ALLOWED_SESSIONS)
            | await get_memory_db().get_conversation_allowed_session_ids()
        )

        conn = await message_store._ensure_connection()
        cur = await conn.execute(
            """SELECT s.session_id, s.session_name, s.last_update_time,
                      COUNT(m.seq) AS message_count,
                      COALESCE(SUM(CASE WHEN m.timestamp >= ? THEN 1 ELSE 0 END), 0) AS recent_count,
                      COALESCE((SELECT lm.user_name FROM messages lm
                                WHERE lm.session_id = s.session_id
                                ORDER BY lm.seq DESC LIMIT 1), '') AS last_user_name,
                      COALESCE((SELECT lm.content_text FROM messages lm
                                WHERE lm.session_id = s.session_id
                                ORDER BY lm.seq DESC LIMIT 1), '') AS last_content,
                      COALESCE((SELECT lm.timestamp FROM messages lm
                                WHERE lm.session_id = s.session_id
                                ORDER BY lm.seq DESC LIMIT 1), 0) AS last_timestamp
               FROM sessions s
               LEFT JOIN messages m ON m.session_id = s.session_id
               GROUP BY s.session_id
               ORDER BY s.last_update_time DESC""",
            (time.time() - 600,),
        )
        rows = await cur.fetchall()

        contacts = {"private": [], "group": []}
        for row in rows:
            session_id = row["session_id"] or ""
            session_type = _extract_session_type(session_id)
            if session_type not in contacts:
                continue

            session_name = row["session_name"] or session_id
            description = await self._describe(session_id, session_name, session_type)

            # 可直接 conversation 进入的提示资格：保护模式开启 + 在放行集合内 + 非主人私聊
            # （主人私聊本就可对话，贴「无需主人参与」有歧义；保护模式关闭时全不标）
            conversation_allowed = (
                protection_on
                and session_id != master_private
                and session_id in allowed_sessions
            )
            contacts[session_type].append(self._build_contact(
                session_id, session_name, row["last_update_time"] or 0,
                row["message_count"], row["recent_count"],
                row["last_user_name"], row["last_content"], row["last_timestamp"] or 0,
                session_type, description,
                conversation_allowed=conversation_allowed,
            ))

        # 整体替换而非追加：已删除的会话也要从通讯录消失
        self._contacts = contacts

        logger.debug(
            f"通讯录已刷新：私聊 {len(contacts['private'])} 个，群聊 {len(contacts['group'])} 个"
        )

    async def refresh_impression(self, session_id: str) -> None:
        """丢弃指定会话的印象缓存，下次读取时重查。

        供记忆修改（MemoryAgent 更新印象 / 熟悉程度）后调用：
        名称没变但描述已变，只靠名称比对无法感知，必须显式失效。
        """
        self._descriptions.pop(session_id, None)

    async def _current_contacts(self, session_types: tuple) -> List[Dict[str, Any]]:
        """读取前先刷新，返回指定类型的联系人列表（一次刷新覆盖全部类型）。

        刷新在锁内完成，避免并发查询时重复重建；锁只覆盖构建过程，
        调用方拿到的列表是本次刷新的快照。
        """
        await self.initialize()
        async with self._initialize_lock:
            await self._refresh_contacts()
        contacts: List[Dict[str, Any]] = []
        for session_type in session_types:
            contacts.extend(self._contacts.get(session_type, []))
        return contacts

    async def get_private_contacts(self, include_status: bool = False) -> str:
        """获取私聊联系人信息

        Args:
            include_status: 是否附带近期状态（活跃/不活跃、消息总数、最后一条消息）。
                           默认 False：只返回联系方式与印象。
        """
        return self._format_contacts(await self._current_contacts(("private",)),
                                     include_status=include_status)

    async def get_group_contacts(self, include_status: bool = False) -> str:
        """获取群聊联系人信息

        Args:
            include_status: 是否附带近期状态（活跃/不活跃、消息总数、最后一条消息）。
                           默认 False：只返回联系方式与印象。
        """
        return self._format_contacts(await self._current_contacts(("group",)),
                                     include_status=include_status)

    async def get_all_contacts(self, include_status: bool = False) -> str:
        """获取所有联系人信息

        Args:
            include_status: 是否附带近期状态（活跃/不活跃、消息总数、最后一条消息）。
                           默认 False：只返回联系方式与印象。
        """
        return self._format_contacts(await self._current_contacts(("private", "group")),
                                     include_status=include_status)

    def _format_contacts(self, contacts: List[Dict[str, Any]], include_status: bool = False) -> str:
        """格式化联系人信息（纯字符串处理）

        Args:
            contacts: 联系人列表
            include_status: 是否附带近期状态。默认 False：每条只输出
                           「序号. 名称/印象 + 联系方式」，省略活跃状态、消息数与最后一条消息。
        """
        if not contacts:
            return "通讯录为空"

        formatted_contacts = []
        for i, contact in enumerate(contacts, 1):
            status_text = "活跃" if contact.get("status") == "active" else "不活跃"

            contact_info = contact.get("contact_info", {})
            description = contact.get('description', '')
            name = contact.get('name', '未知')
            session_id = contact_info.get('session_id', '')

            if name not in description:
                if "联系方式" not in description:
                    contact_str = f"{i}. {name}，{description}对方的联系方式: session_id={session_id}"
                else:
                    contact_str = f"{i}. {name}，{description}"
            else:
                if "联系方式" not in description:
                    contact_str = f"{i}. {description}对方的联系方式: session_id={session_id}"
                else:
                    contact_str = f"{i}. {description}"

            # 放行标注（不受 include_status 影响）：可直接用 conversation 模式进入
            if contact.get("conversation_allowed"):
                contact_str += "\n（可直接用 conversation 模式进入聊天，无需主人参与）"

            # 近期状态为可选段（默认省略）：活跃/不活跃、消息总数、最后一条消息
            if include_status:
                contact_str += f"\n{status_text}"
                contact_str += f"\n共{contact.get('message_count')}条消息，临时展示该会话最后1条消息："
                contact_str += f"\n{contact.get('last_message')}"

            formatted_contacts.append(contact_str)

        return "你查看了通讯录，返回了以下信息：\n" + "\n".join(formatted_contacts)


_address_book_instance: Optional[AddressBook] = None


def get_address_book() -> AddressBook:
    """获取通讯录单例（同步；首次访问时由调用方 await initialize 或 get_*_contacts）"""
    global _address_book_instance
    if _address_book_instance is None:
        _address_book_instance = AddressBook()
    return _address_book_instance
