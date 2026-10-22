# -*- coding: utf-8 -*-
"""会话准入：能不能在这个会话里说话。

权限源与主仓一致（memory/address_book.py 的三源口径）：
  1. memory.db 里 characters / groups 的 allow_conversation = 1
     → 取该实体 contacts 里的每个 session_id
  2. config/settings.py 的 SafetyConfig.CONVERSATION_ALLOWED_SESSIONS 白名单
  3. 保护模式 CONVERSATION_PROTECTION_MODE（只读出来当提示，不参与判定）

**默认全都不许。** 没在放行集合里的会话，发送命令一律拒绝——这是防越权的那道闸。
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional

MEMORY_DB = Path(r"E:\ABYSS\N_Touch\memory\memory.db")
CHAT_DB = Path(r"E:\ABYSS\N_Touch\memory\chat_history\chat_history.db")
SETTINGS = Path(r"E:\ABYSS\N_Touch\config\settings.py")


def _from_memory_db() -> Dict[str, str]:
    """allow_conversation=1 的实体 → 它的 contacts 里的 session_id。"""
    out: Dict[str, str] = {}
    if not MEMORY_DB.exists():
        return out
    con = sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)
    try:
        for table in ("characters", "groups"):
            for name, contacts, allow in con.execute(
                f"select name, contacts, allow_conversation from {table}"
            ):
                if not allow:
                    continue
                for c in json.loads(contacts or "[]"):
                    sid = (c or {}).get("session_id") or ""
                    if sid:
                        out[sid] = f"{name}（{table[:-1]}）"
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _from_settings() -> List[str]:
    """settings.py 里的白名单常量（按字面量解析，不 import 整套配置）。"""
    if not SETTINGS.exists():
        return []
    text = SETTINGS.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"CONVERSATION_ALLOWED_SESSIONS\s*=\s*(\[[^\]]*\])", text)
    if not m:
        return []
    try:
        val = ast.literal_eval(m.group(1))
    except (ValueError, SyntaxError):
        return []
    return [v for v in val if isinstance(v, str)]


def _session_names() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not CHAT_DB.exists():
        return out
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    try:
        for sid, sname in con.execute("select session_id, session_name from sessions"):
            out[sid] = sname or ""
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def allowed() -> Dict[str, str]:
    """session_id → 放行说明。默认空集合 = 哪儿都不许说话。"""
    table = _from_memory_db()
    names = _session_names()
    for sid in _from_settings():
        table.setdefault(sid, "配置白名单")
    # 白名单里的会话可能没有 contacts 记录，名字从会话表补
    out = {}
    for sid, why in table.items():
        nm = names.get(sid, "")
        out[sid] = f"{why}｜{nm}" if nm else why
    return out


def protection_on() -> bool:
    if not SETTINGS.exists():
        return True
    text = SETTINGS.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"CONVERSATION_PROTECTION_MODE\s*=\s*(True|False)", text)
    return (m.group(1) == "True") if m else True


def is_allowed(session_id: str) -> bool:
    return (session_id or "").strip() in allowed()


def allowed_list_text() -> str:
    table = allowed()
    if not table:
        return "（没有任何会话被放行）"
    return "\n".join(f"- {sid}  ← {why}" for sid, why in sorted(table.items()))


def lookup_name(session_id: str) -> str:
    return _session_names().get((session_id or "").strip(), "")
