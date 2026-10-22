# -*- coding: utf-8 -*-
"""反思的产出落到「新内核自己的库」，不写主仓。

理由：主库 memory.db / 见闻录.json 正在被运行中的 Core 读写，实验版不该动。
所以反思写这里，查询时把两边合并（本地新增在前）。

  _run/observations.jsonl   见闻录（梗概，不记具体发言）
  _run/bequests.jsonl       话术经验（只收别人的原话）
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List

RUN = Path(__file__).resolve().parent / "_run"
OBS = RUN / "observations.jsonl"
BEQ = RUN / "bequests.jsonl"


def _append(path: Path, rec: Dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    rec.setdefault("ts", time.time())
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return "ok"


def _load(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def add_observation(text: str, topic: str = "") -> str:
    text = (text or "").strip()
    if len(text) < 8:
        raise ValueError("见闻太短：记梗概和结论，不要只写一句废话")
    if len(text) > 400:
        raise ValueError("见闻太长：只记梗概，别把整段对话抄进来")
    _append(OBS, {"text": text, "topic": (topic or "").strip(), "source": "reflection"})
    return f"已记进见闻录：{text[:24]}…（共 {len(load_observations()) + 1} 条）"


def add_speech_bequest(title: str, summary: str, quote: str = "") -> str:
    title = (title or "").strip()
    summary = (summary or "").strip()
    if not title or not summary:
        raise ValueError("标题和做法都要写")
    if quote and len(quote) < 2:
        raise ValueError("原话太短")
    _append(BEQ, {"title": title, "summary": summary, "quote": quote.strip(), "category": "speech", "retrieval_count": 0})
    return f"已记进话术经验：{title}"


def load_observations() -> List[Dict[str, Any]]:
    return _load(OBS)


def load_bequests() -> List[Dict[str, Any]]:
    return _load(BEQ)
