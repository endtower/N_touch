# -*- coding: utf-8 -*-
"""一个循环。没有决策层与说话层的接缝，也没有 conversation_agent。

一次轮次 = 一次生成 = 两样产物（inner / speech）+ 至多一个动作。
该会话的新消息由 Feed 每拍自动推到上下文里——她不"申请了解情况"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import parse_output, leak_check, ProtocolError
from .prompt import build_prompt
from .history import render_messages

# 效益分档 → 效能感增量（沿用 main 的六档口径）
BENEFIT_DELTA = {"+10": 10, "0": 0, "-5": -5, "-10": -10, "-20": -20, "-30": -30}


@dataclass
class Episode:
    session: str
    trigger: str
    max_cycles: int = 6
    cycles: List[Dict[str, Any]] = field(default_factory=list)
    stop_reason: str = ""


def run(
    episode: Episode,
    model,
    state,
    toolbox,
    outbox,
    log,
    name: str = "她",
    recycle_inner: bool = False,
    feed=None,
    warmup: int = 30,
) -> Dict[str, Any]:
    """跑一轮。

    feed：给了就每拍自动拉新消息；不给就用 episode.trigger 当全部现场（离线演示用）。
    recycle_inner=False（默认）：inner 永不回到上下文——这是"过滤"的硬实现。
    """
    context: List[Dict[str, str]] = [
        {"role": "system", "content": build_prompt(name, state.summary(), episode.trigger)}
    ]

    if feed is not None:
        # 游标没设过就推到最新：场景用 recent 只看不动，之后只推真正新到的。
        if episode.session not in feed._last_ts:
            feed.seek_end(episode.session)
        history = feed.recent(episode.session, warmup)
        scene = (
            "【现场·这个会话最近的消息】\n" + render_messages(history)
            if history
            else f"【现场】{episode.trigger}"
        )
        context.append({"role": "user", "content": scene})
    else:
        context.append({"role": "user", "content": f"【现场】{episode.trigger}"})

    budget = episode.max_cycles

    while budget > 0:
        budget -= 1

        # 自动拉取：这一段取代了 Conversation 的"把消息汇报回去"
        pulled = 0
        if feed is not None:
            fresh = feed.drain(episode.session)
            pulled = len(fresh)
            if fresh:
                context.append({"role": "user", "content": "【新消息】\n" + render_messages(fresh)})

        raw = model.complete(context, toolbox.schemas())
        try:
            out = parse_output(raw)
        except ProtocolError:
            raise  # 硬约定：协议不合格不静默容错

        inner, speech, act = out["inner"], out["speech"], out["act"]
        leak = leak_check(inner, speech)

        self_effects: Dict[str, Any] = {}
        # 说话本身不再改情绪。情绪只由 feel（认定发生了什么）和 end_episode（收尾结算）驱动。
        # 原来这里有一句"说了一句 DA+1"，那是在情绪还是仪表的时候顺手加的，现在不成立了。
        log.append(
            "cycle",
            {
                "session": episode.session,
                "pulled": pulled,
                "inner": inner,
                "speech": speech,
                "act": act,
                "leak": leak,
            },
        )

        if speech:
            outbox.emit(episode.session, speech)
            context.append({"role": "assistant", "content": speech})
        if recycle_inner and inner.get("want"):
            context.append({"role": "assistant", "content": f"（我刚才想要：{inner['want']}）"})

        tool_result = None
        if act:
            tool_result = toolbox.call(act["tool"], act.get("args") or {})
            context.append({"role": "user", "content": f"【工具结果】{tool_result}"})

        episode.cycles.append(
            {
                "inner": inner,
                "speech": speech,
                "act": act,
                "leak": leak,
                "tool_result": tool_result,
                "pulled": pulled,
            }
        )

        if act and act["tool"] == "end_episode":
            args = act.get("args") or {}
            delta = BENEFIT_DELTA.get(_grade(args.get("benefit", "0")), 0)
            self_effects["feedback_competence"] = state.feedback["competence"] + delta
            state.transition(log, "episode_end", str(args.get("reason", ""))[:80], **self_effects)
            episode.stop_reason = "settled"
            break

        if self_effects:
            state.transition(log, "spoke", f"第{len(episode.cycles)}拍说话", **self_effects)

        if not speech and not act:
            episode.stop_reason = "idle"
            break

        # 低把握 → 缩短反馈周期，而不是延长独白
        if inner.get("confidence") == "low" and budget > 1:
            budget = min(budget, 1)

    if not episode.stop_reason:
        episode.stop_reason = "budget"

    log.append("episode", {"session": episode.session, "stop": episode.stop_reason, "cycles": len(episode.cycles)})
    return {"stop_reason": episode.stop_reason, "cycles": episode.cycles, "state": state.dump()}


def _grade(raw: Any) -> str:
    text = str(raw).strip()
    if text in BENEFIT_DELTA:
        return text
    for key in BENEFIT_DELTA:
        if key.lstrip("+") and key.lstrip("+") in text:
            return key
    return "0"
