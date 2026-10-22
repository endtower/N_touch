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
    st_e2e = State()
    log_e2e = EventLog(RUN_DIR / "events.jsonl")
    box = build_toolbox(log_e2e, recall=Recall(), dry=True, state=st_e2e)
    check("conversation_agent 已消失", "conversation_agent" not in box.tools, str(list(box.tools)))
    check("十二个工具", len(box.tools) == 12, str(list(box.tools)))
    resident = [n for n, t in box.tools.items() if not t.lazy]
    lazy = sorted(n for n, t in box.tools.items() if t.lazy)
    check("常驻九个", len(resident) == 9, str(resident))
    check("冷库三个", lazy == ["note_record", "sticker_adjust", "sticker_save"], str(lazy))
    sent = [s["function"]["name"] for s in box.schemas()]
    check("冷库不进上下文", all(n not in sent for n in lazy), str(sent))
    for gone in ("wait", "note", "look_up", "look_up_group", "recall_experience", "send_message", "send_image"):
        check(f"{gone} 已删", gone not in box.tools)

    print("[5] 冷库与媒体")
    box.call("fetch_tool", {"name": "note_record"})
    check("fetch 后进上下文", "note_record" in [s["function"]["name"] for s in box.schemas()])
    box.call("note_record", {"text": "自检写入的一条", "category": "selftest"})
    out = box.call("note_query", {"keyword": "自检"})
    check("记事本能取回", "自检写入的一条" in out, out[:60])
    out = box.call("sticker_list", {"limit": "3"})
    check("表情包库能列", "张" in out or "空" in out, out[:60])
    viewer = box.call("sticker_view", {"ref": "粉发萌宠精灵"})
    check("读图返回图片包", isinstance(viewer, dict) and viewer.get("__image__"), str(viewer)[:60])

    print("[6] 认定情绪")
    from .feelings import available_acts, feel
    acts = available_acts()
    check("认得到主仓行为表", len(acts) >= 10, str(len(acts)))
    out = box.call("feel", {"act": acts[0]})
    check("返回当前情绪", "【当前情绪】" in out and "DA" in out, out[:60])
    try:
        box.call("feel", {"act": acts[0]})
        check("同拍重复认定应拒绝", False)
    except ValueError:
        check("同拍重复认定应拒绝", True)
    try:
        box.call("feel", {"act": "不存在的判定"})
        check("未知认定应拒绝", False)
    except ValueError:
        check("未知认定应拒绝", True)

    print("[7] 命令式发送")
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
        st_e2e,
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
