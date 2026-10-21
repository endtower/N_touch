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
    """通讯录类，用于管理和查询联系人信息（纯异步）"""

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._storage_path = data_root() / "memory" / "pfca" / "address_book"
            self._storage_path.mkdir(parents=True, exist_ok=True)
            self._contacts = {
                "private": [],
                "group": []
            }
            self._loaded = False
            self._initialized = True
            self._initialize_lock = None  # 在第一次异步调用时建

    def _build_contact(self, session_id: str, session_name: str,
                       last_update_time: float, message_list: List[Dict],
                       session_type: str, description: str,
                       conversation_allowed: bool = False) -> Dict[str, Any]:
        """从单个会话数据构建联系人信息字典（纯字符串处理）

        状态判定：当前最近 10 分钟内消息数 >= 10 → 活跃，否则不活跃

        conversation_allowed：该会话是否可直接用 conversation 模式进入（无需主人参与）。
        由 _load_contacts 传入（保护模式放行集合判定结果），此处仅原样落字段。
        """
        import time
        message_count = len(message_list)

        # 状态判定：当前时间最近 10 分钟内的消息数
        ten_min_ago = time.time() - 600
        recent_count = sum(
            1 for msg in message_list
            if msg.get("timestamp", 0) >= ten_min_ago
        )

        last_message = ""
        if message_list:
            last_msg = message_list[-1]
            user_name = last_msg.get("user_name", "未知")
            content_text = last_msg.get("content_text", "")
            timestamp = last_msg.get("timestamp", 0)
            time_str = datetime.fromtimestamp(timestamp).strftime("%m月%d号 %H:%M:%S")
            last_message = f"[{time_str}] {user_name}:{content_text}"

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
        """异步初始化（首次调用 get_*_contacts 时会 await）"""
        if self._loaded:
            return
        if self._initialize_lock is None:
            import asyncio
            self._initialize_lock = asyncio.Lock()
        async with self._initialize_lock:
            if self._loaded:
                return
            await self._load_contacts()

    async def _load_contacts(self):
        """异步加载联系人信息（通过 MessageRepository 读取，不再直接读 JSON 文件）

        加载失败直接抛异常阻断（硬约定 #4），不吞异常伪装"空通讯录"。
        """
        from brain.hc.database.character_compose import CharacterCompose
        from brain.hc.database.core import get_memory_db
        from config.settings import SafetyConfig
        from config.role_settings import RoleConfig
        from memory.message_repository import get_message_store
        character_compose = CharacterCompose()
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

        # 通过 repository 列出所有会话
        sessions = await message_store.list_sessions()

        for session_info in sessions:
            session_id = session_info.get("session_id", "")
            session_name = session_info.get("session_name", session_id)
            last_update_time = session_info.get("last_update_time", 0)
            session_type = _extract_session_type(session_id)
            if session_type not in ("private", "group"):
                continue

            # 通过 repository 读取会话消息
            session_data = await message_store._get_session_data(session_id)
            message_list = session_data.get("message_list", [])

            try:
                impression_result = await character_compose.first_session_impression_search(
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
                logger.error(f"获取第一印象失败: {e}")
                if session_type == "private":
                    description = f"你对{session_name}没有任何印象。"
                else:
                    description = f"群组{session_name}。"

            # 可直接 conversation 进入的提示资格：保护模式开启 + 在放行集合内 + 非主人私聊
            # （主人私聊本就可对话，贴「无需主人参与」有歧义；保护模式关闭时全不标）
            conversation_allowed = (
                protection_on
                and session_id != master_private
                and session_id in allowed_sessions
            )
            contact = self._build_contact(
                session_id, session_name, last_update_time,
                message_list, session_type, description,
                conversation_allowed=conversation_allowed,
            )
            self._contacts[session_type].append(contact)

        self._loaded = True

    async def get_private_contacts(self, include_status: bool = False) -> str:
        """获取私聊联系人信息

        Args:
            include_status: 是否附带近期状态（活跃/不活跃、消息总数、最后一条消息）。
                           默认 False：只返回联系方式与印象。
        """
        await self.initialize()
        contacts = self._contacts.get("private", [])
        return self._format_contacts(contacts, include_status=include_status)

    async def get_group_contacts(self, include_status: bool = False) -> str:
        """获取群聊联系人信息

        Args:
            include_status: 是否附带近期状态（活跃/不活跃、消息总数、最后一条消息）。
                           默认 False：只返回联系方式与印象。
        """
        await self.initialize()
        contacts = self._contacts.get("group", [])
        return self._format_contacts(contacts, include_status=include_status)

    async def get_all_contacts(self, include_status: bool = False) -> str:
        """获取所有联系人信息

        Args:
            include_status: 是否附带近期状态（活跃/不活跃、消息总数、最后一条消息）。
                           默认 False：只返回联系方式与印象。
        """
        await self.initialize()
        private_contacts = self._contacts.get("private", [])
        group_contacts = self._contacts.get("group", [])
        all_contacts = private_contacts + group_contacts
        return self._format_contacts(all_contacts, include_status=include_status)

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
