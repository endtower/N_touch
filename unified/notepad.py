# -*- coding: utf-8 -*-
"""记事本（新内核自带的一份，暂不与主仓 shortlight 记事本共用）。

主仓那份是章节式落库 + render_manual，结构重；这一版先用 append-only JSONL，
够验证"能不能记住并取回来"。要接主仓，把 NOTES 换成它的读接口即可。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

NOTES = Path(__file__).resolve().parent / "_run" / "notepad.jsonl"


def _load() -> List[Dict[str, Any]]:
    if not NOTES.exists():
        return []
    out = []
    for line in NOTES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def record(text: str, category: str = "", title: str = "") -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError("记的内容不能为空")
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": time.time(),
        "title": (title or text[:12]).strip(),
        "category": (category or "general").strip(),
        "text": text,
    }
    with NOTES.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return f"已记下：{rec['title']}（{rec['category']}）"


def query(keyword: str = "", limit: int = 5) -> str:
    rows = _load()
    if not rows:
        return "记事本还是空的。"
    q = (keyword or "").strip()
    hits = [r for r in rows if not q or q in r.get("text", "") or q in r.get("title", "") or q in r.get("category", "")]
    hits = hits[-int(limit):]
    if not hits:
        return f"记事本里没有关于「{q}」的记录。"
    lines = [f"记事本里 {len(rows)} 条，这里是 {len(hits)} 条："]
    for r in hits:
        lines.append(f"- [{r.get('category','')}] {r.get('title','')}：{r.get('text','')}")
    return "\n".join(lines)
