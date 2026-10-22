# -*- coding: utf-8 -*-
"""硬规则自检。没有 pytest 也能跑：python -m unified.selftest

检查四件事：
1. 协议不合格必须失败（不许静默容错）
2. 泄露检测能抓到"把算盘说出口"
3. 状态事务失败必须整体回滚
4. 整条链路能跑到 settled
"""

from __future__ import annotations

import sys
from pathlib import Path

from .model import parse_output, leak_check, ProtocolError
from .state import State, EventLog, Outbox
from .tools import build_toolbox
from .model import MockModel
from .loop import run, Episode
from .history import Recall
from .replay import DEMO_SCRIPT

RUN_DIR = Path(__file__).resolve().parent / "_selftest"
ok, fail = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  ok   {name}")
    else:
        fail += 1
        print(f"  FAIL {name}  {detail}")


def main() -> int:
    print("[1] 协议校验")
    bad_cases = [
        '{"speech":"x"}',
        '{"inner":{"read":"a","want":"b","confidence":"sure"}}',
        "not json",
        '{"inner":{"want":"b","confidence":"low"}}',
    ]
    for c in bad_cases:
        try:
            parse_output(c)
            check(f"应拒绝: {c[:36]}", False)
        except ProtocolError:
            check(f"应拒绝: {c[:36]}", True)
    good = '{"inner":{"read":"a","want":"b","confidence":"low"},"speech":""}'
    check("应接受合法输出", parse_output(good)["speech"] == "")

    print("[2] 泄露检测")
    r1 = leak_check({"read": "先试探他", "want": "套话", "fear": "被看穿", "confidence": "medium"}, "我准备先问问他最近怎么样")
    check("抓到策略词", r1["suspicious"], str(r1))
    r2 = leak_check({"read": "他今天很僵", "want": "确认", "fear": "让他难堪", "confidence": "low"}, "你今天是不是有点累")
    check("正常发言不误报", not r2["suspicious"], str(r2))

    print("[3] 状态事务回滚")
    st = State()
    log = EventLog(RUN_DIR / "rollback.jsonl")
    before = st.dump()
    try:
        st.transition(log, "bad", "测试", mood_da=5, 不存在的字段=1)
        check("应抛错", False)
    except KeyError:
        check("回滚后状态不变", st.dump() == before)

    print("[4] 端到端")
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    for f in ("events.jsonl", "outbox.jsonl"):
        p = RUN_DIR / f
        if p.exists():
            p.unlink()
    box = build_toolbox(EventLog(RUN_DIR / "events.jsonl"), recall=Recall(), dry=True)
    check("conversation_agent 已消失", "conversation_agent" not in box.tools, str(list(box.tools)))
    check("只留三个工具", len(box.tools) == 3, str(list(box.tools)))
    for gone in ("wait", "note", "look_up", "look_up_group", "recall_experience", "send_message", "send_image"):
        check(f"{gone} 已删", gone not in box.tools)
    check("cmd 在册", "cmd" in box.tools)

    print("[5] 命令式发送")
    from .qq_cli import run_command
    out = run_command("qq text group_123 在呢", dry=True)
    check("text 命令可解析", "[dry]" in out and "在呢" in out, out)
    out = run_command("qq poke group_123 456", dry=True)
    check("群戳走 group_poke", "group_poke" in out and "456" in out, out)
    out = run_command("qq poke private_789", dry=True)
    check("私聊戳走 friend_poke", "friend_poke" in out, out)
    try:
        run_command("qq fly group_123", dry=True)
        check("未知动作应报错", False)
    except ValueError:
        check("未知动作应报错", True)
    res = run(
        Episode(session="group_selftest", trigger="他在群里一直说客套话"),
        MockModel(list(DEMO_SCRIPT)),
        State(),
        box,
        Outbox(RUN_DIR / "outbox.jsonl"),
        EventLog(RUN_DIR / "events.jsonl"),
    )
    check("跑到 settled", res["stop_reason"] == "settled", res["stop_reason"])
    check("三拍都对上了", len(res["cycles"]) == 3, str(len(res["cycles"])))
    check("效能感已结算", res["state"]["feedback"]["competence"] == 50, str(res["state"]["feedback"]))

    print(f"\n通过 {ok}，失败 {fail}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
