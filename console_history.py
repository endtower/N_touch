"""core 停止时只读访问真实会话库；不建库、不迁移、不实例化业务组件。"""

import json
import sqlite3
from contextlib import closing

from paths import data_root


def _image_url(field):
    if not isinstance(field, str) or not field:
        return None
    from pathlib import Path
    basename = field.replace("\\", "/").rsplit("/", 1)[-1]
    for folder, prefix in (("memory/chat_history/media", "/media/"), ("memory/llm_save", "/llm-save/")):
        root = (data_root() / folder).resolve()
        if not root.is_dir():
            continue
        # 旧记录可能带绝对路径；只返回位于当前数据目录的已有媒体。
        candidate = Path(field)
        matches = [candidate] if candidate.is_absolute() and candidate.is_file() else root.rglob(basename)
        for match in matches:
            resolved = match.resolve()
            if resolved.is_file() and resolved.is_relative_to(root):
                return prefix + resolved.relative_to(root).as_posix()
    return None


def _connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def list_sessions() -> list[dict]:
    root = data_root() / "memory"
    history = root / "chat_history" / "chat_history.db"
    if not history.exists():
        return []
    names = {}
    memory = root / "memory.db"
    if memory.exists():
        with closing(_connect(memory)) as conn:
            for scope, table in (("private", "characters"), ("group", "groups")):
                for name, aliases_json, contacts_json in conn.execute(
                    f"SELECT name, aliases, contacts FROM {table} ORDER BY uuid"
                ):
                    aliases = json.loads(aliases_json or "[]")
                    contacts = json.loads(contacts_json or "[]")
                    first = aliases[0] if isinstance(aliases, list) and aliases else None
                    display = (first.strip() if isinstance(first, str) else "") or name
                    for contact in contacts if isinstance(contacts, list) else []:
                        sid = contact.get("session_id") if isinstance(contact, dict) else None
                        if display and isinstance(sid, str) and sid.startswith(scope + "_"):
                            names.setdefault(sid, display)
    with closing(_connect(history)) as conn:
        rows = conn.execute("SELECT session_id, session_name, last_update_time FROM sessions ORDER BY last_update_time DESC").fetchall()
    sessions = []
    for sid, name, updated in rows:
        scope, _, bare_id = sid.partition("_")
        if scope not in ("private", "group"):
            continue
        display = names.get(sid) or name or bare_id
        sessions.append({"session_id": bare_id, "scope": scope, "user_id": bare_id,
                         "user_name": display, "session_name": display, "role": None,
                         "created_at": updated or 0})
    return sessions


def read_messages(scope: str, bare_id: str, limit: int = 50) -> dict:
    if scope not in ("private", "group") or not bare_id:
        raise ValueError("scope 和 id 无效")
    if not 0 <= limit <= 10000:
        raise ValueError("limit 必须在 0–10000 范围内")
    path = data_root() / "memory" / "chat_history" / "chat_history.db"
    result = {"session_id": bare_id, "scope": scope, "total": 0, "messages": []}
    if not path.exists():
        return result
    with closing(_connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        sid = f"{scope}_{bare_id}"
        result["total"] = conn.execute("SELECT count(*) FROM messages WHERE session_id=?", (sid,)).fetchone()[0]
        query = "SELECT * FROM messages WHERE session_id=? ORDER BY seq DESC"
        rows = conn.execute(query + (" LIMIT ?" if limit else ""), (sid, limit) if limit else (sid,)).fetchall()
    for row in reversed(rows):
        message = dict(row)
        message["content"] = json.loads(message.get("content") or "[]")
        message["images"] = []
        for segment in message["content"] if isinstance(message["content"], list) else []:
            if isinstance(segment, dict) and segment.get("type") == "image":
                data = segment.get("data", segment)
                if isinstance(data, dict):
                    url = _image_url(data.get("file"))
                    if url:
                        message["images"].append(url)
        result["messages"].append(message)
    return result
