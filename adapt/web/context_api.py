# -*- coding: utf-8 -*-
"""
adapt.web.context_api
=====================

LLM 上下文查看接口.

源数据:
  - memory/chat/conversation.json             (当前活跃, list of {role, content})
  - memory/chat/conversation_archived_*.json  (历史归档)
  - memory/pfca/active.json                   (当前活跃)
  - memory/pfca/pfca_archived_*.json          (历史归档)
  - memory/proactive/active.json             (当前活跃)
  - memory/proactive/proactive_archived_*.json(历史归档)
  - memory/social_agent/social_agent.json     (当前活跃)
  - memory/social_agent/social_agent_archived_*.json (历史归档)
  - memory/social_chat/social_chat.json       (当前活跃)
  - memory/social_chat/social_chat_archived_*.json   (历史归档)

端点:
  GET /context/files?kind=chat|pfca|proactive|social_agent|social_chat -> {"files": [...]}
  GET /context/file?kind=...&name=xxx -> {"name": ..., "entries": [...], "raw_size": ...}

只读, 不写; 不会触碰任何文件.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List

from aiohttp import web

from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


# 允许的上下文目录 (跟源码目录一一对应)
_KIND_DIRS = {
    "chat": "chat",
    "pfca": "pfca",
    "proactive": "proactive",
    "social_agent": "social_agent",
    "social_chat": "social_chat",
}

# 各自目录的活跃文件名
_ACTIVE_FILE = {
    "chat": "conversation.json",
    "pfca": "active.json",
    "proactive": "active.json",
    "social_agent": "social_agent.json",
    "social_chat": "social_chat.json",
}

# 归档文件名前缀
_ARCHIVE_PREFIX = {
    "chat": "conversation_archived_",
    "pfca": "pfca_archived_",
    "proactive": "proactive_archived_",
    "social_agent": "social_agent_archived_",
    "social_chat": "social_chat_archived_",
}

# 安全: 名字必须只允许字母数字, _ . - (防 path traversal)
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.json$")


def _project_root() -> Path:
    """项目根 = adapt/web/context_api.py 的三倍父目录."""
    return Path(__file__).resolve().parents[2]


def _memory_dir() -> Path:
    return _project_root() / "memory"


def _resolve_kind_dir(kind: str) -> Path:
    sub = _KIND_DIRS.get(kind)
    if not sub:
        raise ValueError(f"kind 必须是 chat|pfca|proactive|social_agent|social_chat")
    return _memory_dir() / sub


def _list_files(kind: str) -> List[Dict[str, Any]]:
    d = _resolve_kind_dir(kind)
    if not d.exists():
        return []
    active_name = _ACTIVE_FILE[kind]
    out: List[Dict[str, Any]] = []
    for entry in d.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if not name.endswith(".json"):
            continue
        if name == active_name:
            out.append({
                "name": name,
                "is_active": True,
                "size": entry.stat().st_size,
                "mtime": entry.stat().st_mtime,
            })
        elif name.startswith(_ARCHIVE_PREFIX[kind]):
            out.append({
                "name": name,
                "is_active": False,
                "size": entry.stat().st_size,
                "mtime": entry.stat().st_mtime,
            })
    # 活跃的放最前, 归档按 mtime 倒序
    out.sort(key=lambda x: (not x["is_active"], -x["mtime"]))
    return out


def _read_file(kind: str, name: str) -> Dict[str, Any]:
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(f"name 不合法: {name!r}")
    d = _resolve_kind_dir(kind)
    fp = d / name
    if not fp.exists() or not fp.is_file():
        raise FileNotFoundError(f"{fp} 不存在")
    # 防止符号链接外溢: resolve 后必须仍在 d 内
    fp_resolved = fp.resolve()
    d_resolved = d.resolve()
    try:
        fp_resolved.relative_to(d_resolved)
    except ValueError:
        raise PermissionError(f"拒绝: {name} 超出 {d}")
    raw = fp.read_text(encoding="utf-8")
    size = len(raw.encode("utf-8"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")
    # 把原始 entry 的所有字段都透传出来 (保留 tool_calls / reasoning_content /
    # name / tool_call_id, 前端要做 LLM→Tool 配对卡片时要这些字段)
    # 注意: 只保留 JSON 可序列化的字段, None 就保留 None, 不强行 str()
    entries: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            entry: Dict[str, Any] = {}
            for k, v in item.items():
                # 基本类型直接透传, 复杂类型用 json 强转保底
                if isinstance(v, (str, int, float, bool)) or v is None or isinstance(v, list) or isinstance(v, dict):
                    entry[k] = v
                else:
                    try:
                        entry[k] = json.loads(json.dumps(v, ensure_ascii=False, default=str))
                    except Exception:
                        pass
            # role 缺省给空字符串
            entry.setdefault("role", "")
            entries.append(entry)
    elif isinstance(data, dict):
        # PFCA 极少出现 dict 形态, 也兜一下
        entry = {k: v for k, v in data.items()
                 if isinstance(v, (str, int, float, bool)) or v is None or isinstance(v, list) or isinstance(v, dict)}
        entry.setdefault("role", "")
        entries.append(entry)
    return {
        "kind": kind,
        "name": name,
        "is_active": name == _ACTIVE_FILE[kind],
        "entries": entries,
        "raw_size": size,
    }


# ============================================================
# aiohttp handlers
# ============================================================

async def handle_list_files(request: web.Request) -> web.Response:
    try:
        kind = request.query.get("kind", "").strip()
        if not kind:
            return web.json_response({"error": "kind 必填 (chat|pfca|proactive|social_agent|social_chat)"}, status=400)
        try:
            files = _list_files(kind)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({
            "kind": kind,
            "files": files,
            "fetched_at": time.time(),
        })
    except Exception as e:
        logger.exception("[/context/files] 失败")
        return web.json_response({"error": f"列出文件失败: {e}"}, status=500)


async def handle_read_file(request: web.Request) -> web.Response:
    try:
        kind = request.query.get("kind", "").strip()
        name = request.query.get("name", "").strip()
        if not kind or not name:
            return web.json_response({"error": "kind 和 name 必填"}, status=400)
        try:
            data = _read_file(kind, name)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except PermissionError as e:
            return web.json_response({"error": str(e)}, status=403)
        return web.json_response(data)
    except Exception as e:
        logger.exception("[/context/file] 失败")
        return web.json_response({"error": f"读取文件失败: {e}"}, status=500)
