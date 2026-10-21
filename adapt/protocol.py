#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.protocol
=============

平台无关的合约层。

包含：
    - Platform / SessionScope 枚举
    - MessageSegment  通用消息段（type + data）
    - NormalizedEvent 归一化事件（Core 内部唯一可见的事件形状）
    - OutboundTarget  出站目标
    - OutboundPayload 出站载荷
    - Channel         Channel 协议（所有平台实现的统一接口）

这些类型构成了 Core 与外部 IM 平台之间的"操作系统 ABI"。
任何平台实现（onebot / wechat / ...）都必须把平台事件 → NormalizedEvent，
把 OutboundPayload → 平台原生消息段，反之亦然。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, runtime_checkable
from .bridge.contract import SendReceipt


# ============================================================
# 枚举
# ============================================================

class Platform(str, Enum):
    """外部 IM 平台标识。"""
    QQ = "qq"
    WECHAT = "wechat"
    WEB = "web"  # 浏览器前端（测试 / 替代 NapCat 用）

    @classmethod
    def parse(cls, value: str) -> "Platform":
        """宽松解析：大小写、空格、'onebot' 等同 'qq'。"""
        v = (value or "").strip().lower()
        if v in ("qq", "onebot", "napcat", "go-cqhttp"):
            return cls.QQ
        if v in ("wechat", "wx", "gewechat", "comwechat"):
            return cls.WECHAT
        if v in ("web", "browser", "frontend"):
            return cls.WEB
        raise ValueError(f"unknown platform: {value!r}")


class SessionScope(str, Enum):
    """会话作用域。

    约定：与 core.message_handler 规范一致，
    session_id 统一为 "{scope}_{id}" 格式。
    """
    PRIVATE = "private"
    GROUP = "group"
    CHANNEL = "channel"   # 预留：频道/群组中的子频道


# ============================================================
# 通用消息段
# ============================================================

def _attachment_basename(d: Dict[str, Any]) -> str:
    """从附件段 data 中提取原始文件名（basename），用于在 content_text 里展示。

    优先用 ``file`` 字段，fallback 到 ``name``，去掉可能的路径前缀。
    命中不到时返回空串，由调用方决定是否回退到无文件名的占位符。
    """
    raw = (d.get("file") or d.get("name") or "")
    if not raw:
        return ""
    # 兼容正反斜杠路径，只保留最后一段
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    if "\\" in raw:
        raw = raw.rsplit("\\", 1)[-1]
    return str(raw)


@dataclass
class MessageSegment:
    """通用消息段。type + data 的最小约定。

    之所以用一个 dataclass 而不是 dict，是因为：
    1. 类型提示更友好，IDE 跳转更准
    2. 可以挂方法（比如 to_text()）
    3. 序列化为 dict 时用 asdict() 即可

    data 的内容由 type 决定，常见：
        - "text":   {"text": "..."}
        - "at":     {"qq": "123456"} 或 {"user_id": "..."}
        - "image":  {"file": "...", "url": "..."}
        - "reply":  {"id": "msg_id"}
        - "face":   {"id": "..."}
        - "record": {"file": "...", "url": "..."}
        - "video":  {"file": "...", "url": "..."}
        - "file":   {"file": "...", "name": "..."}
        - "forward": {"id": "..."}
    """
    type: str
    data: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"type": self.type, "data": dict(self.data)}

    def to_text(self) -> str:
        """将本段转成可读文本（用于 content_text 字段）。

        附件类段（image / file / voice / video）会带上原始文件名，
        让 LLM 通过扩展名判断文件类型，需要时再调工具查具体内容。
        """
        t = self.type
        d = self.data or {}
        if t == "text":
            return str(d.get("text", ""))
        if t == "at":
            name = d.get("name") or d.get("nickname") or d.get("qq") or d.get("user_id", "")
            return f"@{name}"
        if t == "image":
            fname = _attachment_basename(d)
            return f"[图片:{fname}]" if fname else "[图片]"
        if t == "reply":
            return ""
        if t == "face":
            fid = d.get("id", "")
            return f"[表情:{fid}]" if fid else "[表情]"
        if t in ("record", "voice"):
            fname = _attachment_basename(d)
            return f"[语音:{fname}]" if fname else "[语音]"
        if t == "video":
            fname = _attachment_basename(d)
            return f"[视频:{fname}]" if fname else "[视频]"
        if t == "file":
            fname = d.get("name") or _attachment_basename(d)
            return f"[文件:{fname}]" if fname else "[文件]"
        if t == "forward":
            return "[聊天记录]"
        if t in ("json", "xml"):
            return "[富文本]"
        if t == "poke":
            return "[戳一戳]"
        if t == "share":
            return f"[分享:{d.get('title', '')}]"
        if t == "contact":
            return "[名片]"
        if t == "location":
            return "[位置]"
        if t == "music":
            return "[音乐]"
        return f"[{t}]"


# ============================================================
# 归一化事件
# ============================================================

@dataclass
class NormalizedEvent:
    """平台无关的事件。

    这是 Core 内部唯一可见的"消息"形状。Core 业务层只跟这个类型打交道。

    字段说明：
        platform        来源平台
        scope           会话作用域（private / group / channel）
        session_id      会话 ID，格式 "private_xxx" / "group_xxx" / "channel_xxx"
        session_name    会话名（私聊为对方昵称，群聊为群名）
        user_id         发送者 ID
        user_name       发送者昵称
        message_id      平台消息 ID（字符串）
        timestamp       Unix 秒级时间戳
        segments        消息段列表（保持原顺序）
        content_text    segments 转成的可读文本（content 的纯文本视图）
        raw             平台原始 payload（调试 / 透传通道）

    设计要点：
        - session_id 自身包含 scope（"private_xxx"），下游不需要二次反解
        - content_text 冗余存一份，避免下游每次都要 re-construct
        - raw 永远保留，调试和"订阅原始事件"用得到
    """
    platform: Platform
    scope: SessionScope
    session_id: str
    session_name: str
    user_id: str
    user_name: str
    message_id: str
    timestamp: int
    segments: List[MessageSegment] = field(default_factory=list)
    content_text: str = ""
    raw: Optional[Dict[str, Any]] = None

    @property
    def conversation_id(self) -> str:
        """不含 scope 前缀的纯会话 ID（"private_xxx".split("_",1)[1]）。"""
        from .session_identity import parse_session_id
        return parse_session_id(self.session_id).identity

    def to_system_message_dict(self) -> Dict[str, Any]:
        """转成 memory.message_handler.process_message 期望的格式。

        目标格式（参见 api/routes.py:130-135 与 memory/message_handler.py）：
            {
                "session_info": {
                    "session_id":   "private_xxx" | "group_xxx",
                    "session_name": "...",
                    "remark":       "",
                },
                "message_list": [
                    {
                        "message_id":   "...",
                        "user_id":      "...",
                        "user_name":    "...",
                        "timestamp":    int,
                        "content_text": "...",
                        "content":      [{"type": ..., "data": ...}, ...],
                    }
                ]
            }
        """
        return {
            "session_info": {
                "session_id": self.session_id,
                "session_name": self.session_name,
                "remark": "",
                "platform": self.platform.value,
                "connection_id": "onebot" if self.platform == Platform.QQ else self.platform.value,
                "remote_session_id": self.session_id,
                "generation": "",
            },
            "message_list": [
                {
                    "message_id": self.message_id,
                    "user_id": self.user_id,
                    "user_name": self.user_name,
                    "timestamp": int(self.timestamp),
                    "content_text": self.content_text,
                    "content": [seg.as_dict() for seg in self.segments],
                }
            ],
        }


# ============================================================
# 出站类型
# ============================================================

@dataclass(frozen=True)
class OutboundTarget:
    """出站目标。"""
    scope: SessionScope
    target_id: str          # 群号 / 用户号 / 微信号
    session_id: Optional[str] = None  # 主程序会话身份；桥接时与远端会话不同

    def to_action_param(self) -> Dict[str, Any]:
        """转成 OneBot 风格 params（其它平台各自实现自己的转换）。"""
        if self.scope == SessionScope.PRIVATE:
            return {"user_id": int(self.target_id)} if self.target_id.isdigit() else {"user_id": self.target_id}
        if self.scope == SessionScope.GROUP:
            return {"group_id": int(self.target_id)} if self.target_id.isdigit() else {"group_id": self.target_id}
        return {"channel_id": self.target_id}


@dataclass
class OutboundPayload:
    """出站载荷。"""
    segments: List[MessageSegment] = field(default_factory=list)
    raw_text: Optional[str] = None       # 兼容纯文本调用方

    @classmethod
    def text(cls, text: str) -> "OutboundPayload":
        return cls(segments=[MessageSegment(type="text", data={"text": text})], raw_text=text)


# ============================================================
# Channel 协议
# ============================================================

@runtime_checkable
class Channel(Protocol):
    """Channel 协议。

    所有平台实现都必须满足此协议。Core 只看 Channel 接口，
    不依赖任何具体平台。
    """
    name: str
    platform: Platform

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send(self, target: OutboundTarget, payload: OutboundPayload) -> str | SendReceipt: ...

    def subscribe(self) -> AsyncIterator[NormalizedEvent]:
        """可选：供需要"原始平台事件流"的订阅者使用。

        大多数情况下 Channel 内部直接调用注入的消息处理器（message_handler），
        不需要外部订阅。保留这个方法是为了将来能透传平台特有事件。
        """
        raise NotImplementedError
