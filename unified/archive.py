# -*- coding: utf-8 -*-
"""「我这儿有什么」——把杂项查询收成一个入口。

分工（按用户的划分）：
  lookup(target)        人物 / 群聊：这是谁、什么印象、什么关系（通讯录）
  browse(query, kind)   话术经验 / 锦囊 / 见闻录 / 笔记 / 表情包

硬约束：**决策经验不启用。** bequests 里 category='decision' 的 21 条一律不查，
只查 category='speech'（也就是话术）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import media, notepad

MEMORY_DB = Path(r"E:\ABYSS\N_Touch\memory\memory.db")
OBSERVATION_FILE = Path(r"E:\ABYSS\N_Touch\brain\hc\shortlight\notes\slots\见闻录.json")

KINDS = ("all", "speech", "kit", "observation", "note", "sticker")


def _con() -> Optional[sqlite3.Connection]:
    if not MEMORY_DB.exists():
        return None
    return sqlite3.connect(f"file:{MEMORY_DB}?mode=ro", uri=True)


# ==================== 通讯录 ====================

CHAT_DB = Path(r"E:\ABYSS\N_Touch\memory\chat_history\chat_history.db")
RANGES = ("私聊", "群聊", "全部")


def _scope_of(session_id: str) -> str:
    if session_id.startswith("group_") or session_id.startswith("wechat_group_"):
        return "group"
    return "private"


def _impression_of(name: str, scope: str) -> str:
    con = _con()
    if con is None or not name:
        return ""
    try:
        if scope == "group":
            row = con.execute(
                "select impression, self_image from groups where name like ? limit 1", (f"%{name}%",)
            ).fetchone()
            if row:
                bits = [b for b in row if b]
                return " / ".join(bits)
        row = con.execute(
            "select impression, relationship from characters where name like ? or aliases like ? limit 1",
            (f"%{name}%", f"%{name}%"),
        ).fetchone()
        if row:
            bits = [b for b in row if b]
            return " / ".join(bits)
    except sqlite3.Error:
        return ""
    finally:
        con.close()
    return ""


def _contacts(scope: str, include_status: bool) -> List[Dict[str, Any]]:
    """从消息库读会话表 → 能联系谁。"""
    if not CHAT_DB.exists():
        return []
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    rows = con.execute(
        """
        select s.session_id, s.session_name, count(m.seq) as n, max(m.timestamp) as last_ts
        from sessions s left join messages m on m.session_id = s.session_id
        group by s.session_id order by n desc
        """
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for sid, sname, n, last_ts in rows:
        sc = _scope_of(sid)
        if scope != "all" and sc != scope:
            continue
        item: Dict[str, Any] = {
            "session_id": sid,
            "name": sname or sid,
            "scope": sc,
            "impression": _impression_of(sname or "", sc),
        }
        if include_status:
            last = con.execute(
                "select user_name, content_text from messages where session_id=? order by seq desc limit 1",
                (sid,),
            ).fetchone()
            item["count"] = n
            item["last"] = f"{last[0]}: {last[1][:40]}" if last and last[1] else ""
        out.append(item)
    con.close()
    return out


def address_book(search_range: str = "全部", include_status: bool = False) -> str:
    """通讯录：能跟谁联系。也可以直接给一个名字，查对那个人的印象。"""
    rng = (search_range or "全部").strip()
    if rng not in RANGES:
        # 不是范围 → 当成具体名字，先查人再查群
        person = _impression_of(rng, "private")
        group = _impression_of(rng, "group")
        if person:
            return f"【人】{rng}\n  {person}"
        if group:
            return f"【群】{rng}\n  {group}"
        return f"通讯录里没有「{rng}」，也查不到对他的印象。"

    scope = {"私聊": "private", "群聊": "group", "全部": "all"}[rng]
    items = _contacts(scope, include_status)
    if not items:
        return "通讯录为空。"
    lines = [f"通讯录（{rng}）：共 {len(items)} 个可联系的会话"]
    for i, c in enumerate(items, 1):
        tag = "群聊" if c["scope"] == "group" else "私聊"
        entry = f"{i}. {c['name']}（{tag}），session_id={c['session_id']}"
        if c["impression"]:
            entry += f"\n   印象：{c['impression']}"
        if include_status:
            entry += f"\n   共{c.get('count', 0)}条；最后一条 {c.get('last', '')}"
        lines.append(entry)
    lines.append("\n要发消息用 cmd：qq text <session_id> <内容>；想细看某人用本工具直接传他的名字。")
    return "\n".join(lines)


def lookup(target: str) -> str:
    """查一个人或一个群：印象、关系、熟悉度、别名。"""
    q = (target or "").strip()
    if not q:
        raise ValueError("要查谁？给个人名或群名")
    con = _con()
    if con is None:
        return "通讯录读不到（memory.db 不在预期位置）"
    like = f"%{q}%"
    out: List[str] = []

    row = con.execute(
        "select name, aliases, impression, familiarity, relationship from characters "
        "where name like ? or aliases like ? limit 1",
        (like, like),
    ).fetchone()
    if row:
        out.append("【人】")
        out.append(f"  名字：{row[0]}")
        if row[1]:
            out.append(f"  别名：{row[1]}")
        if row[2]:
            out.append(f"  印象：{row[2]}")
        if row[3]:
            out.append(f"  熟悉度：{row[3]}")
        if row[4]:
            out.append(f"  关系：{row[4]}")

    grow = con.execute(
        "select name, aliases, impression, member_impression, self_image from groups "
        "where name like ? or aliases like ? limit 1",
        (like, like),
    ).fetchone()
    if grow:
        out.append("【群】")
        out.append(f"  名字：{grow[0]}")
        if grow[2]:
            out.append(f"  印象：{grow[2]}")
        if grow[3]:
            out.append(f"  成员印象：{grow[3]}")
        if grow[4]:
            out.append(f"  我在群里的自我定位：{grow[4]}")
    con.close()

    if not out:
        return f"通讯录里没有「{q}」。"
    return "\n".join(out)


# ==================== 杂项查询 ====================

def _speech(query: str, k: int) -> List[str]:
    con = _con()
    if con is None:
        return []
    q = (query or "").strip()
    # 只取 speech：决策经验不启用
    rows = con.execute(
        "select title, summary, retrieval_count from bequests "
        "where category = 'speech' order by retrieval_count desc"
    ).fetchall()
    con.close()
    hits = [r for r in rows if q and (q in r[0] or q in (r[1] or ""))] or rows
    return [f"- {t}（用过{n}次）\n  {s}" for t, s, n in hits[:k]] if hits else []


def _kits(query: str, k: int) -> List[str]:
    con = _con()
    if con is None:
        return []
    q = (query or "").strip()
    rows = con.execute("select category, dilemma, advice from decision_kits").fetchall()
    con.close()
    hits = [r for r in rows if q and (q in (r[1] or "") or q in (r[2] or ""))] or rows
    return [f"- [{c}] 困境：{d}\n  建议：{a}" for c, d, a in hits[:k]] if hits else []


def _observations(query: str, k: int) -> List[str]:
    if not OBSERVATION_FILE.exists():
        return []
    try:
        data = json.loads(OBSERVATION_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    notes = data.get("notes") or []
    q = (query or "").strip()
    hits = []
    for n in reversed(notes):
        text = (n.get("text") or n.get("content") or "").strip()
        if not text:
            continue
        if q and q not in text:
            continue
        hits.append(text)
        if len(hits) >= k:
            break
    return [f"- {t}" for t in hits]


def browse(query: str = "", kind: str = "all", k: int = 3) -> str:
    """一次看几类：话术经验、锦囊、见闻录、笔记、表情包。

    kind 只用来收窄，默认 all；模型不用它也能用。
    """
    kind = (kind or "all").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"kind 只能是：{'、'.join(KINDS)}")
    want = set(KINDS[1:]) if kind == "all" else {kind}

    sections: List[str] = []
    if "speech" in want:
        rows = _speech(query, k)
        if rows:
            sections.append("【话术经验】\n" + "\n".join(rows))
    if "kit" in want:
        rows = _kits(query, k)
        if rows:
            sections.append("【锦囊】\n" + "\n".join(rows))
    if "observation" in want:
        rows = _observations(query, k)
        if rows:
            sections.append("【见闻录】\n" + "\n".join(rows))
    if "note" in want:
        text = notepad.query(query, k)
        if text and "空的" not in text and "没有关于" not in text:
            sections.append("【记事本】\n" + text)
    if "sticker" in want:
        text = media.list_items("stickers", query, k)
        if text and "空" not in text:
            sections.append("【表情包】\n" + text)

    if not sections:
        tail = f"关于「{query}」" if query else "库里"
        return f"{tail}没找到相关的东西。"
    return "\n\n".join(sections)
