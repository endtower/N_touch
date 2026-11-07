# -*- coding: utf-8 -*-
"""
adapt.web.protocol
==================

WebChannel WS 消息协议: 前后端对接的"契约"。

前端 (web_frontend/src/types.ts) 与本文件必须保持字段一一对应。

消息分两类:
  - ClientMessage: 前端 → 后端
  - ServerMessage: 后端 → 前端

用 Pydantic 2 的 discriminated union (按 `type` 字段路由)。
"""

from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


# ============================================================
# 通用子结构
# ============================================================

class IdentityDTO(BaseModel):
    """身份（一个身份 = (user_id, user_name)）。"""
    model_config = ConfigDict(extra="forbid")

    user_id: str
    user_name: str
    is_default: bool = False
    created_at: float = 0.0


class SessionRuntimeDTO(BaseModel):
    """会话 runtime（前端 store 直接吃这个）。"""
    model_config = ConfigDict(extra="forbid")

    session_id: str
    scope: str                       # "private" | "group"
    active_identity: IdentityDTO
    identity_lib: List[IdentityDTO] = Field(default_factory=list)


# ============================================================
# Client -> Server
# ============================================================

class ClientCreateIdentity(BaseModel):
    """前端: 新建 / 重复时复用身份。"""
    model_config = ConfigDict(extra="forbid")
    type: Literal["create_identity"]
    session_id: str
    user_id: str
    user_name: str
    switch_to: bool = True           # 创建后是否立即切过去


class ClientUpdateIdentity(BaseModel):
    """前端: 改名 / 改 id。
    old_user_id + old_user_name 定位原条目
    new_user_id / new_user_name 至少一个非空, 表示要改成的目标
    """
    model_config = ConfigDict(extra="forbid")
    type: Literal["update_identity"]
    session_id: str
    old_user_id: str
    old_user_name: str
    new_user_id: Optional[str] = None
    new_user_name: Optional[str] = None


class ClientDeleteIdentity(BaseModel):
    """前端: 删除身份（最后一个不允许删）。"""
    model_config = ConfigDict(extra="forbid")
    type: Literal["delete_identity"]
    session_id: str
    user_id: str
    user_name: str


class ClientSwitchIdentity(BaseModel):
    """前端: 切换当前激活身份。"""
    model_config = ConfigDict(extra="forbid")
    type: Literal["switch_identity"]
    session_id: str
    user_id: str
    user_name: str


class ClientRequest(BaseModel):
    """所有前端消息的并集。"""
    model_config = ConfigDict(extra="forbid")

    msg: Annotated[
        Union[
            ClientCreateIdentity,
            ClientUpdateIdentity,
            ClientDeleteIdentity,
            ClientSwitchIdentity,
        ],
        Field(discriminator="type"),
    ]


# ============================================================
# Server -> Client
# ============================================================

class ServerHello(BaseModel):
    """首次连接时的欢迎包。"""
    model_config = ConfigDict(extra="forbid")
    type: Literal["hello"]
    channel: str
    platform: str
    sessions: List[dict]
    runtimes: dict[str, SessionRuntimeDTO] = Field(default_factory=dict)


class ServerSessionRuntime(BaseModel):
    """广播: 某个 session 的 runtime 变了。"""
    model_config = ConfigDict(extra="forbid")
    type: Literal["session_runtime"]
    runtime: SessionRuntimeDTO


class ServerWarning(BaseModel):
    """非致命警告 (例如私聊切身份的提醒)。"""
    model_config = ConfigDict(extra="forbid")
    type: Literal["warning"]
    code: str
    message: str
    session_id: Optional[str] = None


class ServerError(BaseModel):
    """操作失败。"""
    model_config = ConfigDict(extra="forbid")
    type: Literal["error"]
    code: str
    message: str
    session_id: Optional[str] = None


# ============================================================
# 错误码常量
# ============================================================

class ErrorCode:
    IDENTITY_NOT_FOUND      = "identity_not_found"        # (user_id, user_name) 不在库
    DUPLICATE_IDENTITY      = "duplicate_identity"        # 新 (uid, uname) 已存在 (update 时)
    LIB_EMPTY_AFTER_DELETE  = "lib_empty_after_delete"    # 删到 0 个不允许
    SESSION_NOT_FOUND       = "session_not_found"         # session_id 不存在
    PRIVATE_SWITCH          = "private_switch_warning"    # 私聊里切身份的提醒 (非阻塞)


class WarningCode:
    PRIVATE_SWITCH          = "private_switch"            # 私聊切身份, 提示不建议