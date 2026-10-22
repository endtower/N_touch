# -*- coding: utf-8 -*-
"""回放与演示。没有网络也能跑完整链路（MockModel）。

用法：
  python -m unified.replay --demo
  python -m unified.replay --jsonl msgs.jsonl     # 每行一条 {"session":..,"text":..}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .state import State, EventLog, Outbox
from .tools import build_toolbox
from .model import MockModel
from .loop import run, Episode

RUN_DIR = Path(__file__).resolve().parent / "_run"

DEMO_SCRIPT = [
    {
        "inner": {
            "read": "主人今晚说话很僵，像在背稿，不像他平时那样",
            "want": "想确认一下是不是我多心",
            "fear": "直接点破会让他更难堪",
            "confidence": "medium",
        },
        "speech": "他今天怎么这么客气",
        "act": {"tool": "look_up", "args": {"person": "主人"}},
    },
    {
        "inner": {
            "read": "通讯录里写着最近两周他话变少了。可能是累，不是装",
            "want": "把话递给他，但不逼他解释",
            "fear": "问得太直接，他会觉得被监视",
            "confidence": "low",
        },
        "speech": "有点不像你平时说话的样子。是累了吗",
        "act": None,
    },
    {
        "inner": {
            "read": "他没接。再问就是追问了",
            "want": "收手，留个台阶",
            "fear": "把气氛弄成审问",
            "confidence": "high",
        },
        "speech": "算了，你们聊，我去吃饭了",
        "act": {
            "tool": "end_episode",
            "args": {"benefit": "0", "reason": "递了话但对方没回应，没有可指认的实际价值"},
        },
    },
]


def _setup(seed_memory: bool = True):
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("events.jsonl", "outbox.jsonl"):
        p = RUN_DIR / name
        if p.exists():
            p.unlink()
    log = EventLog(RUN_DIR / "events.jsonl")
    outbox = Outbox(RUN_DIR / "outbox.jsonl")
    state = State()
    box = build_toolbox(log)
    if seed_memory:
        box.memory.extend(
            [
                {"speaker": "主人", "text": "今晚群里那个活动我不太想去"},
                {"speaker": "阿远", "text": "他最近好像挺忙"},
            ]
        )
        box.people["主人"] = {"印象": "话不多，怕麻烦别人", "最近": "两周内说话变少"}
        box.people["阿远"] = {"印象": "爱开玩笑，容易接住话"}
    return state, box, log, outbox


def demo() -> int:
    state, box, log, outbox = _setup()
    model = MockModel(list(DEMO_SCRIPT))
    ep = Episode(session="group_demo", trigger="【群消息】阿远：他在群里一直说客套话，不太像他")
    result = run(ep, model, state, box, outbox, log, name="她")

    print("=" * 62)
    print(f"会话 {ep.session}  停止原因：{result['stop_reason']}")
    print("=" * 62)
    for i, c in enumerate(result["cycles"], 1):
        inner = c["inner"]
        print(f"\n[第{i}拍]")
        print(f"  内心 · 读  : {inner.get('read')}")
        print(f"  内心 · 要  : {inner.get('want')}   (把握 {inner.get('confidence')})")
        print(f"  内心 · 怕  : {inner.get('fear')}")
        flag = "  ⚠ 疑似泄露" if c["leak"]["suspicious"] else ""
        print(f"  正文       : {c['speech'] or '（沉默）'}{flag}")
        if c["act"]:
            print(f"  动作       : {c['act']['tool']} → {str(c['tool_result'])[:60]}")
        else:
            print("  动作       : 无")
    print("\n" + "-" * 62)
    print("终态：" + state.summary())
    print(f"模型调用次数：{len(model.calls)}（每次带 {len(box.schemas())} 个工具）")
    print(f"账本：{RUN_DIR / 'events.jsonl'}")
    print(f"出话：{RUN_DIR / 'outbox.jsonl'}")
    return 0


def replay(path: Path) -> int:
    state, box, log, outbox = _setup(seed_memory=False)
    model = MockModel([])  # 无剧本时每轮返回沉默，用于验证链路
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            box.memory.append({"speaker": rec.get("speaker", "?"), "text": rec.get("text", "")})
            ep = Episode(session=rec.get("session", "unknown"), trigger=rec.get("text", ""))
            run(ep, model, state, box, outbox, log)
            n += 1
    print(f"回放 {n} 条，终态：{state.summary()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="unified.replay")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--jsonl", type=Path)
    args = ap.parse_args(argv)
    if args.jsonl:
        return replay(args.jsonl)
    return demo()


if __name__ == "__main__":
    sys.exit(main())
