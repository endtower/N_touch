# -*- coding: utf-8 -*-
"""影子运行器：真模型 + 真会话，但**不发群**，只把她会说的话写出来给你看。

  python -m unified.runner --session group_1095799561 --limit 30 --cycles 2

为什么先影子：发出去是以她的身份、进真群、不可撤回。先读几条，再决定开不开真发。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .state import State, EventLog, Outbox
from .tools import build_toolbox
from .loop import run, Episode
from .history import Feed, Recall
from .live_model import LiveModel
from .replay import _report, _fresh, RUN_DIR

STATE_FILE = RUN_DIR / "state.json"


def load_state() -> State:
    if STATE_FILE.exists():
        return State.load(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    return State()


def save_state(state: State) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state.dump(), ensure_ascii=False, indent=2), encoding="utf-8")


def _self_name(rows) -> str:
    """她在这个会话里叫什么：取她最近一条消息的署名。"""
    for row in reversed(rows):
        if row.get("user_id") == "system":
            return row.get("speaker") or "她"
    return "她"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="unified.runner")
    ap.add_argument("--session", required=True)
    ap.add_argument("--limit", type=int, default=30, help="进现场的历史消息条数")
    ap.add_argument("--cycles", type=int, default=2, help="最多几拍")
    ap.add_argument("--name", default=None, help="她在这个会话里的名字；不给就从历史里认")
    ap.add_argument("--max-tokens", type=int, default=3000)
    ap.add_argument("--assume-sent", action="store_true",
                    help="回执谎报'已发送'，用于测重复行为（影子模式下'未发出'会让她重说）")
    args = ap.parse_args(argv)

    feed = Feed()
    recent = feed.recent(args.session, args.limit)
    if not recent:
        print(f"会话 {args.session} 没有消息")
        return 1
    her = args.name or _self_name(recent)

    print(f"会话 {args.session}  历史 {len(recent)} 条  模型 {LiveModel().model}")
    print("末尾 3 条：")
    for m in recent[-3:]:
        print(f"  {m['speaker']}: {m['text'][:60]}")
    print()

    state = load_state()
    log = EventLog(_fresh("events.jsonl"))
    outbox = Outbox(_fresh("outbox.jsonl"))
    mem = Recall()
    box = build_toolbox(log, dry=True, state=state)  # 影子模式：cmd 一律不发
    model = LiveModel(max_tokens=args.max_tokens)

    ep = Episode(
        session=args.session,
        trigger=f"【现场】{recent[-1]['speaker']}: {recent[-1]['text']}",
        max_cycles=args.cycles,
    )
    def speaker(text: str) -> str:
        """影子模式：不发。回执两种口径：
        - 默认"未真正发出"：安全，但会诱发她把话重说一遍（她知道没落地）
        - --assume-sent：骗她说发出去了，用来测"重复"这个行为本身
        """
        if args.assume_sent:
            return "已发送，平台回执 message_id=shadow-0001"
        return "（影子模式，未真正发出，只写进了 outbox）"

    try:
        res = run(ep, model, state, box, outbox, log, name=her, feed=feed, warmup=args.limit,
                  speaker=speaker, max_speech=2)
    except Exception as exc:  # noqa: BLE001
        print(f"跑挂了：{type(exc).__name__}: {exc}")
        return 2
    finally:
        save_state(state)

    _report(ep, res, model, box, state)
    print("\n（影子模式：以上文字没有发出去，只写进了 unified/_run/outbox.jsonl）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
