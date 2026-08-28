#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.onebot.parser
===================

OneBot v11 事件 → NormalizedEvent 的纯函数。

只关心 schema，不关心网络。可单测：

    ev = parse_onebot_event({
        "post_type": "message",
        "message_type": "group",
        ...
    })
    assert ev.platform == Platform.QQ
    assert ev.scope == SessionScope.GROUP
    assert ev.session_id == "group_87654321"

不在此处理：
    - meta_event（心跳/生命周期）—— 单独 log，不转化为 NormalizedEvent
    - notice（非消息通知）—— 当前不处理，返回 None
    - request（好友/群请求）—— 同上
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from adapt.message_segments import segments_to_text
from adapt.protocol import (
    MessageSegment,
    NormalizedEvent,
    Platform,
    SessionScope,
)
from . import constants as C


# 当前 parser 关注的 post_type 白名单
_HANDLED_POST_TYPES = (C.OB_POST_MESSAGE, C.OB_POST_MESSAGE_SENT)


def parse_onebot_event(raw: Dict[str, Any]) -> Optional[NormalizedEvent]:
    """解析 OneBot 事件。

    Returns:
        NormalizedEvent | None
            - None: 当前不处理的事件类型（meta_event / notice / request）
            - NormalizedEvent: 归一化后的消息事件
    """
    if not isinstance(raw, dict):
        return None

    post_type = raw.get("post_type")
    if post_type not in _HANDLED_POST_TYPES:
        return None

    message_type = raw.get("message_type")
    if message_type not in (C.OB_MSG_PRIVATE, C.OB_MSG_GROUP):
        return None

    # 提取 session 信息
    if message_type == C.OB_MSG_PRIVATE:
        scope = SessionScope.PRIVATE
        user_id = str(raw.get("user_id") or "")
        group_id = ""      # 私聊无 group_id
        session_raw_id = user_id
    else:  # group
        scope = SessionScope.GROUP
        group_id = str(raw.get("group_id") or "")
        user_id = str(raw.get("user_id") or "")
        session_raw_id = group_id

    if not session_raw_id:
        return None
    if not user_id:
        return None

    session_id = f"{scope.value}_{session_raw_id}"

    # 提取发送者信息
    sender = raw.get("sender") or {}
    user_name = (
        sender.get("card")         # 群昵称（在群里显示的名字）
        or sender.get("nickname")  # QQ 昵称
        or user_id
    )

    # 提取消息
    raw_message = raw.get("message")
    segments = _parse_message_segments(raw_message)
    content_text = segments_to_text(segments)

    # 提取消息 ID / 时间戳
    message_id = str(raw.get("message_id") or "")
    timestamp = int(raw.get("time") or time.time())

    # session_name：私聊为对方昵称，群聊为真实群名（NapCat / go-cqhttp 在 message 事件里
    # 实际已经带上了 group_name 字段，少数实现可能没有，没有时回退到占位符）。
    if scope == SessionScope.PRIVATE:
        session_name = user_name
    else:
        group_name = raw.get("group_name")
        if isinstance(group_name, str) and group_name.strip():
            session_name = group_name.strip()
        else:
            session_name = f"群_{session_raw_id}"

    return NormalizedEvent(
        platform=Platform.QQ,
        scope=scope,
        session_id=session_id,
        session_name=session_name,
        user_id=user_id,
        user_name=str(user_name),
        message_id=message_id,
        timestamp=timestamp,
        segments=segments,
        content_text=content_text,
        raw=raw,
    )


def _parse_message_segments(raw_message: Any) -> List[MessageSegment]:
    """把 OneBot 的 message 字段转成通用 MessageSegment 列表。

    兼容两种入参：
        - list[dict]   : array 格式（NapCat 9.9+ 默认）
        - str          : string 格式（含 CQ 码），降级处理成单一 text 段
    """
    if isinstance(raw_message, list):
        out: List[MessageSegment] = []
        for item in raw_message:
            if not isinstance(item, dict):
                continue
            t = str(item.get("type") or "")
            data = item.get("data") or {}
            if not isinstance(data, dict):
                data = {}
            out.append(MessageSegment(type=t, data=dict(data)))
        return out
    if isinstance(raw_message, str):
        # string 格式直接降级为单一 text 段（不解析 CQ 码）
        return [MessageSegment(type=C.OB_SEG_TEXT, data={"text": raw_message})]
    return []
