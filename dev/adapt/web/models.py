# -*- coding: utf-8 -*-
"""
adapt.web.models
================

WebChannel 的数据模型（避免 channel.py 和 db_store.py 互相 import）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class WebSession:
    """一个会话（对应磁盘上一个 chat_history 文件）。"""
    session_id: str                              # 裸 ID（不带 scope 前缀）
    scope: str                                  # "private" | "group"
    user_id: str                                # 会话身份 ID（= 私聊对方 QQ / 群号）
    user_name: str                              # 会话显示名
    session_name: str = ""                      # 会话显示名（与 user_name 重复，保留兼容）
    role: Optional[str] = None                  # 群聊中扮演的角色（私聊 None）
    platform: str = ""                          # "qq" | "wechat" | "web"，旧会话无前缀按 qq
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ============================================================
# 身份 (Identity)
# ============================================================
# 设计: 身份由 (user_id, user_name) 联合主键确定。
#   - 同 user_id 不同 user_name = 不同条目 (别名/历史身份), 可以并存
#   - 改名字 = update_identity
#   - user_id 即 QQ 号, user_name 即在群里显示的名字
#   - 私聊默认身份 = (session_id, session_name)
#   - 群聊默认身份 = (RoleConfig.MASTER_ID, RoleConfig.MASTER_NAME)
# ============================================================

@dataclass
class Identity:
    """会话内一个「可扮演的身份」。"""
    user_id: str                                # ID（QQ 号之类）
    user_name: str                              # 显示名（角色 / 别名）
    is_default: bool = False                    # 是否是初始化时种下的默认身份
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def key(user_id: str, user_name: str) -> str:
        return f"{user_id}\x00{user_name}"


@dataclass
class SessionRuntime:
    """一个会话的运行时状态: 当前激活身份 + 可选身份库。"""
    session_id: str                              # 裸 ID
    scope: str                                  # "private" | "group"
    active_identity: Identity                    # 当前发消息用的身份
    identity_lib: List[Identity] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "scope": self.scope,
            "active_identity": self.active_identity.to_dict(),
            "identity_lib": [i.to_dict() for i in self.identity_lib],
        }