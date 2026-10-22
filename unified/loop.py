# -*- coding: utf-8 -*-
"""一个循环。没有决策层与说话层的接缝，没有 handoff，没有 purpose 压缩。

一次轮次 = 一次生成 = 两样产物（inner / speech）+ 至多一个动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .model import parse_output, leak_check, ProtocolError
from .prompt import build_prompt

# 效益分档 → 效能感增量（沿用 main 的六档口径）
BENEFIT_DELTA = {"+10": 10, "0": 0, "-5": -5, "-10": -10, "-20": -20, "-30": -30}


@dataclass
class Episode:
    """一次被唤醒到收尾。"""

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
) -> Dict[str, Any]:
    """跑一轮。

    recycle_inner=False（默认）：inner 永不回到上下文。这是"过滤"的硬实现——
        代价是她在这段会话里不会记得自己刚才的算盘，只记得说过什么。
    recycle_inner=True：把 inner.want 写回上下文。用于 A/B 对照，看策略记忆值不值那点污染。
    """
    context: List[Dict[str, str]] = [{"role": "system", "content": build_prompt(name, state.summary(), episode.trigger)}]
    budget = episode.max_cycles

    while budget > 0:
        budget -= 1
        raw = model.complete(context, toolbox.schemas())
        try:
            out = parse_output(raw)
        except ProtocolError:
            raise  # 硬约定：协议不合格不静默容错

        inner, speech, act = out["inner"], out["speech"], out["act"]
        leak = leak_check(inner, speech)

        # 1) 内心 → 只进状态与审计，不进群
        self_effects: Dict[str, Any] = {}
        if speech:
            self_effects["mood_da"] = 1
        log.append(
            "cycle",
            {
                "session": episode.session,
                "inner": inner,
                "speech": speech,
                "act": act,
                "leak": leak,
            },
        )

        # 2) 说出去
        if speech:
            outbox.emit(episode.session, speech)
            context.append({"role": "assistant", "content": speech})
        if recycle_inner and inner.get("want"):
            context.append({"role": "assistant", "content": f"（我刚才想要：{inner['want']}）"})

        # 3) 动作
        tool_result = None
        if act:
            tool_result = toolbox.call(act["tool"], act.get("args") or {})
            context.append({"role": "user", "content": f"【工具结果】{tool_result}"})

        episode.cycles.append(
            {"inner": inner, "speech": speech, "act": act, "leak": leak, "tool_result": tool_result}
        )

        # 4) 状态结算
        if act and act["tool"] == "end_episode":
            args = act.get("args") or {}
            delta = BENEFIT_DELTA.get(_grade(args.get("benefit", "0")), 0)
            self_effects["feedback_competence"] = state.feedback["competence"] + delta
            if self_effects:
                state.transition(log, "episode_end", str(args.get("reason", ""))[:80], **self_effects)
            episode.stop_reason = "settled"
            break
        if self_effects:
            state.transition(log, "spoke", f"第{len(episode.cycles)}拍说话", **self_effects)

        # 5) 终止：既没说话也没动作 → 没什么要做的
        if not speech and not act:
            episode.stop_reason = "idle"
            break

        # 6) 低把握 → 缩短反馈周期，而不是延长独白
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
    # 容忍 "＋10" / "+10分" 这类写法，但不容忍编造档位
    for key in BENEFIT_DELTA:
        if key.lstrip("+") and key.lstrip("+") in text:
            return key
    return "0"
