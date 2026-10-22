# -*- coding: utf-8 -*-
"""把 main 的数据接到统一 agent 上——只读，仓库里那两份 db 一律不写。

三样东西：
  Feed       每次唤醒/每拍自动拉该会话的最新消息（取代 Conversation 的"汇报"职责）
  Experience 相关经验检索（取代 conversation_agent 这个动作）
  People     人物印象与关系（取代"先问一句他是谁"）
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

CHAT_DB = Path(r"E:\ABYSS\N_Touch\memory\chat_history\chat_history.db")
MEMORY_DB = Path(r"E:\ABYSS\N_Touch\memory\memory.db")


def _con(path: Path) -> Optional[sqlite3.Connection]:
    if not path.exists():
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


class Feed:
    """按游标拉新消息。她不"申请开会话"，消息自己会到。"""

    def __init__(self, db: Path = CHAT_DB):
        self.db = db
        self._last_ts: Dict[str, float] = {}

    def drain(self, session: str, limit: int = 20) -> List[Dict[str, Any]]:
        con = _con(self.db)
        if con is None:
            return []
        last = self._last_ts.get(session, 0.0)
        rows = con.execute(
            """
            select user_name, content_text, timestamp from messages
            where session_id = ? and timestamp > ? and coalesce(content_text,'') <> ''
            order by seq asc limit ?
            """,
            (session, last, limit),
        ).fetchall()
        con.close()
        out = []
        for name, text, ts in rows:
            out.append({"speaker": name or "", "text": text, "ts": ts})
            self._last_ts[session] = max(self._last_ts.get(session, 0.0), ts)
        return out

    def recent(self, session: str, n: int = 40) -> List[Dict[str, Any]]:
        """只看，不动游标。场景用的就是它。"""
        con = _con(self.db)
        if con is None:
            return []
        rows = con.execute(
            """
            select user_name, content_text, timestamp from messages
            where session_id = ? and coalesce(content_text,'') <> ''
            order by seq desc limit ?
            """,
            (session, n),
        ).fetchall()
        con.close()
        rows.reverse()
        return [{"speaker": r[0] or "", "text": r[1], "ts": r[2]} for r in rows]

    def seek_end(self, session: str) -> None:
        """把游标推到最新一条：之后只推真正新到的消息。"""
        rows = self.recent(session, 1)
        if rows:
            self._last_ts[session] = float(rows[-1]["ts"])


class Experience:
    """经验 = bequests（学到的做法）+ decision_kits（困境配的建议）。

    recall() 先按关键词精确找，找不到就退回"最常用的几条"——
    因为"我没有相关经验"本身就是个坏答案，她该有点底子。
    """

    def __init__(self, db: Path = MEMORY_DB):
        self.db = db
        self._rows: Optional[List[tuple]] = None
        self._kits: Optional[List[tuple]] = None

    def _load(self) -> None:
        con = _con(self.db)
        if con is None:
            self._rows, self._kits = [], []
            return
        self._rows = con.execute(
            "select title, category, summary, retrieval_count from bequests order by retrieval_count desc"
        ).fetchall()
        self._kits = con.execute("select category, dilemma, advice from decision_kits").fetchall()
        con.close()

    def recall(self, query: str, k: int = 3) -> str:
        if self._rows is None:
            self._load()
        q = (query or "").strip()
        hits = [r for r in self._rows if q and (q in r[0] or q in (r[2] or ""))]
        if not hits:
            hits = self._rows[:k]  # 退路：最常用的
        hits = hits[:k]
        kits = [x for x in self._kits if q and (q in (x[1] or "") or q in (x[2] or ""))][:2]
        if not hits and not kits:
            return "没有相关的经验。"

        lines = []
        if kits:
            lines.append("【锦囊】")
            lines += [f"- 困境：{d}\n  建议：{a}" for _, d, a in kits]
        if hits:
            lines.append("【经验】")
            for title, cat, summary, n in hits:
                lines.append(f"- {title}（{cat}，用过{n}次）\n  {summary}")
        return "\n".join(lines)

    def count(self) -> int:
        if self._rows is None:
            self._load()
        return len(self._rows or [])


class People:
    """人物印象。真名、别名、熟悉度、关系一句话。"""

    def __init__(self, db: Path = MEMORY_DB):
        self.db = db

    def get(self, name: str) -> Dict[str, Any]:
        con = _con(self.db)
        if con is None:
            return {}
        row = con.execute(
            "select name, aliases, impression, familiarity, relationship from characters where name like ? limit 1",
            (f"%{name}%",),
        ).fetchone()
        con.close()
        if not row:
            return {}
        return {"name": row[0], "aliases": row[1], "impression": row[2], "familiarity": row[3], "relationship": row[4]}

    def get_group(self, name: str) -> Dict[str, Any]:
        con = _con(self.db)
        if con is None:
            return {}
        row = con.execute(
            "select name, impression, member_impression, self_image from groups where name like ? limit 1",
            (f"%{name}%",),
        ).fetchone()
        con.close()
        if not row:
            return {}
        return {"name": row[0], "impression": row[1], "members": row[2], "self_image": row[3]}


def render_messages(msgs: List[Dict[str, Any]], limit: int = 20) -> str:
    if not msgs:
        return "（暂时没有新消息）"
    return "\n".join(f"{m['speaker']}: {m['text']}" for m in msgs[-limit:])


def as_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
