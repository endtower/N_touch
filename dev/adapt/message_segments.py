#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.message_segments
======================

通用消息段构造工具。

调用方使用这些工厂函数构造 MessageSegment，不直接写字面量。
后续若需要扩展（如 at_segment 改名、加新段类型），只改这里。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

from .protocol import MessageSegment


# ============================================================
# 段工厂
# ============================================================

def text_segment(text: str) -> MessageSegment:
    return MessageSegment(type="text", data={"text": text})


def at_segment(qq_or_user_id: str, name: str = "") -> MessageSegment:
    """@ 段。name 是可选的展示名（部分平台不展示）。"""
    data: Dict[str, Any] = {"qq": qq_or_user_id}
    if name:
        data["name"] = name
    return MessageSegment(type="at", data=data)


def image_segment(file: str = "", url: str = "") -> MessageSegment:
    data: Dict[str, Any] = {}
    if file:
        data["file"] = file
    if url:
        data["url"] = url
    return MessageSegment(type="image", data=data)


def reply_segment(message_id: str) -> MessageSegment:
    return MessageSegment(type="reply", data={"id": message_id})


def face_segment(face_id: str) -> MessageSegment:
    return MessageSegment(type="face", data={"id": face_id})


def record_segment(file: str = "", url: str = "") -> MessageSegment:
    data: Dict[str, Any] = {}
    if file:
        data["file"] = file
    if url:
        data["url"] = url
    return MessageSegment(type="record", data=data)


# ============================================================
# 工具
# ============================================================

def segments_to_text(segments: Iterable[MessageSegment]) -> str:
    """把段列表转成可读文本。

    规则见 MessageSegment.to_text()。
    """
    out: List[str] = []
    for seg in segments:
        piece = seg.to_text()
        if piece:
            out.append(piece)
    return "".join(out)


def normalize_segments(segments: List[MessageSegment]) -> List[MessageSegment]:
    """轻量规范化：去重连续空 text 段、过滤空 reply 段。"""
    cleaned: List[MessageSegment] = []
    for seg in segments:
        if seg.type == "text" and not (seg.data.get("text") or "").strip():
            continue
        cleaned.append(seg)
    return cleaned
