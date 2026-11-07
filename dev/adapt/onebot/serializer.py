#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.onebot.serializer
=======================

通用 MessageSegment 列表 → OneBot v11 消息段列表。

由于 OneBot v11 的消息段 schema（type + data）和我们
adapt.protocol.MessageSegment 几乎完全一致，所以这里是 pass-through。
保留此模块的目的：
    1. 隔离 OneBot 细节，方便将来要做的字段映射/重命名
    2. 提供"只接 array 格式"的约束入口
"""

from __future__ import annotations

from typing import Any, Dict, List

from adapt.protocol import MessageSegment


def serialize_to_onebot(segments: List[MessageSegment]) -> List[Dict[str, Any]]:
    """通用段列表 → OneBot 11 array 格式。

    Args:
        segments: 通用 MessageSegment 列表

    Returns:
        list[dict]: OneBot 11 array 格式，可直接放进 send_*_msg 的 message 字段
    """
    return [seg.as_dict() for seg in segments]
