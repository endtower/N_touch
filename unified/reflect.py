# -*- coding: utf-8 -*-
"""反思：把刚才的经历变成以后能用的东西。

和说话共用同一个 run() 骨架，只换两样：提示词、工具集。
输出不对外（silent=True），产出写进新内核自己的库（_run/observations.jsonl 等）。

原料是她自己的账本：每一拍的 inner（怎么想、要什么、怕什么）都在 events.jsonl 里，
反思就是读自己的账，判断哪一件事值得留下。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import List

from .loop import Episode, run
from .live_model import LiveModel
from .prompt import build_reflection_prompt
from .state import EventLog, Outbox, State
from .tools import build_toolbox

RUN = Path(__file__).resolve().parent / "_run"
CHAT_DB = Path(r"E:\ABYSS\N_Touch\memory\chat_history\chat_history.db")


def _her_name(default: str = "她") -> str:
    if not CHAT_DB.exists():
        return default
    con = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
    row = con.execute(
        "select user_name from messages where user_id='system' order by seq desc limit 1"
    ).fetchone()
    con.close()
    return (row[0] if row and row[0] else default)


def collect_material(n: int = 40) -> str:
    """从账本里取最近 n 拍的内心记录。"""
    log = EventLog(RUN / "events.jsonl")
    sessions = [e for e in log.tail(400) if e.get("kind") == "session"]
    events = [e for e in log.tail(400) if e.get("kind") == "cycle"][-n:]
    if not sessions and not events:
        return "（最近没有可复盘的记录）"
    lines: List[str] = []
    for e in events:
        inner = e.get("inner") or {}
        sid = e.get("session", "")
        lines.append(f"- 场景 {sid}")
        lines.append(f"  想：{inner.get('read', '')}")
        lines.append(f"  要：{inner.get('want', '')}")
        if inner.get("fear"):
            lines.append(f"  怕：{inner['fear']}")
        if e.get("speech"):
            lines.append(f"  说：{e['speech']}")
        act = e.get("act") or {}
        if act:
            lines.append(f"  做：{act.get('tool')} {json.dumps(act.get('args') or {}, ensure_ascii=False)}")
        if e.get("receipt"):
            lines.append(f"  回执：{e['receipt']}")
    return "\n".join(lines) if lines else "（最近没有可复盘的记录）"


def reflect(n: int = 40, cycles: int = 3, name: str = "", max_tokens: int = 3000) -> int:
    # 复盘不改情绪起点：沿用说话那轮留下的状态
    state_file = RUN / "state.json"
    if state_file.exists():
        try:
            state = State.load(json.loads(state_file.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            state = State()
    else:
        state = State()
    her = name or _her_name()
    log = EventLog(RUN / "events.jsonl")
    outbox = Outbox(RUN / "outbox.jsonl")
    box = build_toolbox(log, dry=True, state=state, mode="reflect")
    model = LiveModel(max_tokens=max_tokens)

    material = collect_material(n)
    print(f"复盘材料：最近 {material.count('- 场景')} 拍")
    print("-" * 62)

    ep = Episode(session="reflection", trigger=material, max_cycles=cycles)
    res = run(
        ep, model, state, box, outbox, log,
        name=her, prompt_builder=build_reflection_prompt, silent=True,
    )

    wrote = 0
    for c in res["cycles"]:
        inner = c.get("inner") or {}
        print(f"[第{res['cycles'].index(c) + 1}拍] 想：{inner.get('read', '')[:60]}")
        act = c.get("act") or {}
        if act:
            print(f"        做：{act.get('tool')}  {json.dumps(act.get('args') or {}, ensure_ascii=False)[:120]}")
            wrote += 1
        if c.get("speech"):
            print(f"        （写了话但复盘不对外，已丢弃）{c['speech'][:40]}")
    print("-" * 62)
    print(f"停止：{res['stop_reason']}    动作 {wrote} 次")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="unified.reflect")
    ap.add_argument("--n", type=int, default=40, help="复盘最近几拍的内心记录")
    ap.add_argument("--cycles", type=int, default=3)
    ap.add_argument("--name", default="")
    ap.add_argument("--max-tokens", type=int, default=3000)
    args = ap.parse_args(argv)
    return reflect(args.n, args.cycles, args.name, args.max_tokens)


if __name__ == "__main__":
    raise SystemExit(main())
