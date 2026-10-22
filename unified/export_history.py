# -*- coding: utf-8 -*-
"""把真实群聊历史导成 JSONL，供 replay 使用。只读，绝不写库。

  python -m unified.export_history --sessions
  python -m unified.export_history --session group_xxx --limit 200 --out unified/_run/sample.jsonl
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DEFAULT_DB = Path(r"E:\ABYSS\N_Touch\memory\chat_history\chat_history.db")


def _connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(f"数据库不存在: {db}")
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def list_sessions(db: Path, min_rows: int = 1) -> None:
    con = _connect(db)
    rows = con.execute(
        """
        select s.session_id, s.session_name, count(m.seq) as n
        from sessions s left join messages m on m.session_id = s.session_id
        group by s.session_id having n >= ? order by n desc
        """,
        (min_rows,),
    ).fetchall()
    for sid, name, n in rows:
        print(f"{n:>7}  {sid}  {name or ''}")
    con.close()


def busiest_session(db: Path) -> str:
    con = _connect(db)
    row = con.execute(
        "select session_id, count(*) n from messages group by session_id order by n desc limit 1"
    ).fetchone()
    con.close()
    if not row:
        raise SystemExit("库里没有消息")
    return row[0]


def export(db: Path, session: str, limit: int, out: Path, tail: bool = True) -> int:
    con = _connect(db)
    order = "desc" if tail else "asc"
    rows = con.execute(
        f"""
        select user_name, content_text, timestamp from messages
        where session_id = ? and coalesce(content_text,'') <> ''
        order by seq {order} limit ?
        """,
        (session, limit),
    ).fetchall()
    con.close()
    rows = list(reversed(rows)) if tail else rows

    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for user_name, text, ts in rows:
            fh.write(
                json.dumps({"session": session, "speaker": user_name or "", "text": text, "ts": ts}, ensure_ascii=False)
                + "\n"
            )
    return len(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="unified.export_history")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--sessions", action="store_true", help="列出会话及消息数")
    ap.add_argument("--session", help="会话 id；不给则取消息最多的一条")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--head", action="store_true", help="取前 limit 条（默认取最近）")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "_run" / "sample.jsonl")
    args = ap.parse_args(argv)

    if args.sessions:
        list_sessions(args.db)
        return 0

    session = args.session or busiest_session(args.db)
    n = export(args.db, session, args.limit, args.out, tail=not args.head)
    print(f"导出 {n} 条（{session}）→ {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
