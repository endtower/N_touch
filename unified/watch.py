# -*- coding: utf-8 -*-
"""常驻看门狗：只盯一个会话，只在有真人新消息时动，冷却内不动。

  # 先干跑一次（不发，只打印她会说什么）
  python -m unified.watch --once

  # 真发，跑起来
  python -m unified.watch --send

刹车（这套架构没有注意力仲裁，必须外挂）：
  --cooldown 30     同一会话两次开口至少隔 30 秒
  --max-speech 2    一轮最多说两条，超出的丢掉
  轮次固定 2 拍，不让她同一条消息连续追问
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

from .state import State, EventLog, Outbox
from .tools import build_toolbox
from .loop import run, Episode
from .history import Feed, Recall
from .live_model import LiveModel, _env
from .sender import Sender

RUN_DIR = Path(__file__).resolve().parent / "_run"
META = RUN_DIR / "watch.json"


def _load() -> Dict[str, Any]:
    if META.exists():
        return json.loads(META.read_text(encoding="utf-8"))
    return {"last_ts": time.time(), "state": None}


def _save(meta: Dict[str, Any]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    META.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _her_name(feed: Feed, session: str) -> str:
    for row in reversed(feed.recent(session, 40)):
        if row["user_id"] == "system":
            return row["speaker"] or "她"
    return "她"


def _episode(session: str, new: List[Dict[str, Any]], feed: Feed, state: State, box, outbox, log, model, name: str, cycles: int, speaker=None, max_speech: int = 2):
    lines = "\n".join(f"{m['speaker']}: {m['text']}" for m in new[-6:])
    ep = Episode(session=session, trigger=f"【新消息】\n{lines}", max_cycles=cycles)
    return run(
        ep, model, state, box, outbox, log,
        name=name, feed=None, speaker=speaker, max_speech=max_speech,
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="unified.watch")
    ap.add_argument("--session", default="private_1787164785")
    ap.add_argument("--interval", type=float, default=4.0)
    ap.add_argument("--cooldown", type=float, default=30.0)
    ap.add_argument("--max-speech", type=int, default=2)
    ap.add_argument("--cycles", type=int, default=2)
    ap.add_argument("--max-tokens", type=int, default=1200)
    ap.add_argument("--send", action="store_true", help="真的发出去；不给就是干跑")
    ap.add_argument("--once", action="store_true", help="只处理最近一条真人消息就退出")
    args = ap.parse_args(argv)

    feed = Feed()
    meta = _load()
    state = State.load(meta["state"]) if meta.get("state") else State()
    log = EventLog(RUN_DIR / "events.jsonl")
    outbox = Outbox(RUN_DIR / "outbox.jsonl")
    mem = Recall()
    box = build_toolbox(log, recall=mem, dry=not args.send, state=state)
    model = LiveModel(max_tokens=args.max_tokens)
    sender = Sender(dry=not args.send)
    name = _her_name(feed, args.session)

    def speaker(text: str) -> str:
        """真发一句，返回回执——回执会回灌给她，让她知道这句到底出去了没有。"""
        result = sender.send(args.session, text)
        if sender.dry:
            return "（干跑，未真正发出）"
        mid = (result or {}).get("message_id", "?")
        return f"已发送，平台回执 message_id={mid}"

    print(f"[watch] 会话 {args.session}  她={name}  模型={model.model}")
    print(f"[watch] 发送={'真发' if args.send else '干跑'}  ingress={'ok' if sender.health() else '连不上'}")
    print(f"[watch] 冷却 {args.cooldown}s  每轮最多 {args.max_speech} 条  轮次 {args.cycles}")

    if args.once:
        rows = feed.recent(args.session, 20)
        humans = [r for r in rows if r["user_id"] != "system"]
        if not humans:
            print("[watch] 没有真人消息")
            return 1
        res = _episode(args.session, humans[-2:], feed, state, box, outbox, log, model, name, args.cycles,
                       speaker=speaker, max_speech=args.max_speech)
        _print(res)
        meta["state"] = state.dump()
        _save(meta)
        return 0

    last_open = 0.0
    while True:
        rows = feed.recent(args.session, 20)
        new = [r for r in rows if float(r["ts"]) > float(meta["last_ts"]) and r["user_id"] != "system"]
        if not new:
            time.sleep(args.interval)
            continue
        if time.time() - last_open < args.cooldown:
            time.sleep(args.interval)
            continue

        meta["last_ts"] = max(float(r["ts"]) for r in rows)
        print(f"\n[watch] {time.strftime('%H:%M:%S')} 收到 {len(new)} 条：{new[-1]['speaker']}: {new[-1]['text'][:40]}")
        try:
            res = _episode(args.session, new, feed, state, box, outbox, log, model, name, args.cycles,
                           speaker=speaker, max_speech=args.max_speech)
            _print(res)
            meta["state"] = state.dump()
        except Exception as exc:  # noqa: BLE001
            print(f"[watch] 本轮失败：{type(exc).__name__}: {exc}")
        _save(meta)
        last_open = time.time()
        time.sleep(args.interval)


def _print(res) -> None:
    """只负责显示。发送已经在 loop 里做完了（带回调执）。"""
    for c in res["cycles"]:
        print(f"  内心: {c['inner'].get('want')}  ({c['inner'].get('confidence')})")
        if not c["speech"]:
            print("  正文: （沉默）")
            continue
        receipt = c.get("receipt") or "（无回执）"
        print(f"  正文: {c['speech']}   ← {receipt}")
    if res["stop_reason"]:
        print(f"  停止：{res['stop_reason']}")


if __name__ == "__main__":
    sys.exit(main())
