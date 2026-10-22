# -*- coding: utf-8 -*-
"""回放 / 演示。

  python -m unified.replay --demo                          # 离线假数据，验证链路
  python -m unified.replay --session group_xxx --limit 20  # 真会话：自动拉消息 + 真经验库
  python -m unified.replay --jsonl msgs.jsonl              # 纯消息回放

三种都不需要网络：模型用 MockModel 按剧本产出协议输出。换成真模型只需替换 model。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .state import State, EventLog, Outbox
from .tools import build_toolbox
from .model import MockModel
from .loop import run, Episode
from .history import Feed, Recall, render_messages

RUN_DIR = Path(__file__).resolve().parent / "_run"

# 离线剧本：用 recall_experience 取经验，然后自己开口
DEMO_SCRIPT = [
    {
        "inner": {
            "read": "群里有人说我说话生硬、人机味重",
            "want": "先别辩解，认一句，再给个当下的动作",
            "fear": "越解释越像在自证",
            "confidence": "medium",
        },
        "speech": "",
        "act": {"tool": "recall", "args": {"query": "生硬", "k": "2"}},
    },
    {
        "inner": {
            "read": "经验说：别堆甜话自证，认一句+给当下动作",
            "want": "照着一句认下来，然后停在那儿",
            "fear": "话说多了又变成表演",
            "confidence": "high",
        },
        "speech": "是我没顾上你的感觉。现在我不走，你说",
        "act": None,
    },
    {
        "inner": {
            "read": "话递出去了，剩下的等对方",
            "want": "收手",
            "fear": "追一句就成催了",
            "confidence": "high",
        },
        "speech": "",
        "act": {"tool": "end_episode", "args": {"benefit": "0", "reason": "递了话，对方还没回应，无可指认的实际价值"}},
    },
]


def _fresh(name: str) -> Path:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    p = RUN_DIR / name
    if p.exists():
        p.unlink()
    return p


def demo() -> int:
    log = EventLog(_fresh("events.jsonl"))
    outbox = Outbox(_fresh("outbox.jsonl"))
    state = State()
    mem = Recall()
    box = build_toolbox(log, recall=mem)
    model = MockModel(list(DEMO_SCRIPT))
    ep = Episode(session="group_demo", trigger="【群消息】有人：你说话怎么这么像机器人")
    res = run(ep, model, state, box, outbox, log, name="她")
    print(f"经验库载入：{mem.exp.count()} 条（0 说明 memory.db 不在预期位置）")
    return _report(ep, res, model, box, state)


def live(session: str, limit: int) -> int:
    feed = Feed()
    mem = Recall()
    log = EventLog(_fresh("events.jsonl"))
    outbox = Outbox(_fresh("outbox.jsonl"))
    state = State()
    box = build_toolbox(log, recall=mem)

    recent = feed.recent(session, limit)
    if not recent:
        print(f"会话 {session} 没有消息（或 db 路径不对）")
        return 1
    # 回到温暖点之前，让 warmup 段成为"现场"
    os.environ.setdefault("UNIFIED_LIVE", "1")
    print(f"会话 {session}：最近 {len(recent)} 条，末尾 3 条：")
    print(render_messages(recent[-3:], 3))
    print(f"经验库：{mem.exp.count()} 条\n")

    # 把游标退回到倒数第 3 条之前，模拟"她正在说话时又进来几条"——
    # 这样第一拍的自动拉取能真的拉到东西，而不是只走一遍空分支。
    if len(recent) >= 4:
        feed._last_ts[session] = float(recent[-4]["ts"]) - 0.001

    model = MockModel(list(DEMO_SCRIPT))
    ep = Episode(session=session, trigger=f"【现场】{recent[-1]['speaker']}: {recent[-1]['text']}")
    res = run(ep, model, state, box, outbox, log, name="她", feed=feed, warmup=limit)
    return _report(ep, res, model, box, state)


def _report(ep, res, model, box, state) -> int:
    print("=" * 62)
    print(f"会话 {ep.session}  停止原因：{res['stop_reason']}")
    print("=" * 62)
    for i, c in enumerate(res["cycles"], 1):
        inner = c["inner"]
        pull = f"  (自动拉到 {c['pulled']} 条)" if c.get("pulled") else ""
        print(f"\n[第{i}拍]{pull}")
        print(f"  内心 · 读  : {inner.get('read')}")
        print(f"  内心 · 要  : {inner.get('want')}   (把握 {inner.get('confidence')})")
        print(f"  内心 · 怕  : {inner.get('fear')}")
        flag = "  ⚠ 疑似泄露" if c["leak"]["suspicious"] else ""
        print(f"  正文       : {c['speech'] or '（沉默）'}{flag}")
        if c["act"]:
            print(f"  动作       : {c['act']['tool']} → {str(c['tool_result'])[:90]}")
        else:
            print("  动作       : 无")
    print("\n" + "-" * 62)
    print("终态：" + state.summary())
    print(f"模型调用 {len(model.calls)} 次，每次带 {len(box.schemas())} 个工具")
    return 0


def replay_jsonl(path: Path) -> int:
    log = EventLog(_fresh("events.jsonl"))
    outbox = Outbox(_fresh("outbox.jsonl"))
    state = State()
    box = build_toolbox(log)
    model = MockModel([])  # 无剧本：每轮沉默，只验证链路
    n = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            feed = Feed()
            feed._last_ts[rec.get("session", "unknown")] = float(rec.get("ts") or 0) - 1
            ep = Episode(session=rec.get("session", "unknown"), trigger=rec.get("text", ""))
            run(ep, model, state, box, outbox, log, feed=feed, warmup=1)
            n += 1
    print(f"回放 {n} 条，终态：{state.summary()}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="unified.replay")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--jsonl", type=Path)
    ap.add_argument("--session", help="真实会话 id，例如 group_1095799561")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args(argv)
    if args.jsonl:
        return replay_jsonl(args.jsonl)
    if args.session:
        return live(args.session, args.limit)
    return demo()


if __name__ == "__main__":
    sys.exit(main())
