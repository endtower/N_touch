# -*- coding: utf-8 -*-
"""显式状态：取代 GlobalSessionState._data 那套"约定 + 单例 reset"。

三条硬规则：
1. 状态只能通过命名事务（transition）改变，不能直接改字段。
2. 每次事务都写进 append-only 事件日志，带 before/after/why。
3. 事务失败必须整体回滚，不留半截状态。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List

AXES = ("da", "ne")
FEEDBACK = ("competence", "belonging")


def _clamp(v: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, int(v)))


@dataclass
class State:
    """一处存放，可整体 dump。没有隐藏的全局单例。"""

    mood: Dict[str, int] = field(default_factory=lambda: {"da": 50, "ne": 50})
    feedback: Dict[str, int] = field(default_factory=lambda: {"competence": 50, "belonging": 50})
    relations: Dict[str, Dict[str, int]] = field(default_factory=dict)
    threads: List[Dict[str, Any]] = field(default_factory=list)
    seq: int = 0
    # 上一次认定的行为（防同一件事连拍重复认定，把情绪打满）
    last_feel: str = ""
    # 已获准进入、可以发言的会话（必须经 enter_session 申请）
    grants: List[str] = field(default_factory=list)

    # ---------- 命名事务 ----------

    def transition(self, log: "EventLog", name: str, why: str, **changes: Any) -> Dict[str, Any]:
        """应用一次变更并记账；任何异常前不落盘。

        changes 支持三种写法：
          mood_da=+5 / mood_ne=-3          增量
          feedback_competence=40           绝对值
          relation_<name>_affinity=+2      关系轴增量
        """
        before = asdict(self)
        try:
            for key, value in changes.items():
                self._apply(key, value)
        except Exception:
            self.__dict__.update(State(**before).__dict__)  # 回滚
            raise
        self.seq += 1
        record = {
            "seq": self.seq,
            "ts": time.time(),
            "name": name,
            "why": why,
            "changes": changes,
            "before": before,
            "after": asdict(self),
        }
        log.append("transition", record)
        return record

    def _apply(self, key: str, value: Any) -> None:
        if key.startswith("relation_"):
            _, name, axis = key.split("_", 2)
            slot = self.relations.setdefault(name, {"familiarity": 0, "affinity": 0})
            slot[axis] = _clamp(slot.get(axis, 0) + int(value))
        elif key.startswith("mood_"):
            axis = key[5:]
            if axis not in AXES:
                raise KeyError(f"未知情绪轴: {axis}")
            self.mood[axis] = _clamp(self.mood[axis] + int(value))
        elif key.startswith("feedback_"):
            axis = key[9:]
            if axis not in FEEDBACK:
                raise KeyError(f"未知反馈轴: {axis}")
            self.feedback[axis] = _clamp(int(value))
        elif key == "threads":
            self.threads = list(value)
        else:
            raise KeyError(f"未知状态字段: {key}")

    # ---------- 序列化 ----------

    def dump(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def load(cls, data: Dict[str, Any]) -> "State":
        return cls(**data)

    def summary(self) -> str:
        m, f = self.mood, self.feedback
        rel = ", ".join(f"{k}(熟{v['familiarity']}/亲{v['affinity']})" for k, v in self.relations.items()) or "无"
        return (
            f"情绪 DA={m['da']} NE={m['ne']} | 效能={f['competence']} 归属={f['belonging']} | 关系 {rel}"
        )


class EventLog:
    """append-only JSONL。一份可回放、可审计的账。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, payload: Dict[str, Any]) -> None:
        line = json.dumps({"kind": kind, "ts": time.time(), **payload}, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def tail(self, n: int = 20) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]


@dataclass
class Outbox:
    """说出去的话单独落一份——用于事后对比 inner 与 speech。"""

    path: Path

    def emit(self, session: str, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "session": session, "text": text}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
