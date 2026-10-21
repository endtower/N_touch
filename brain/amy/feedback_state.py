"""决策侧效能感与归属感。旧 {value} 数据迁入归属感，效能感默认 50。"""
from paths import data_root
import json
import copy
import math
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

from brain.amy.constants import (
    clamp,
    FEEDBACK_DEFAULT, FEEDBACK_MIN, FEEDBACK_MAX,
    TaskResultAct,
)

HISTORY_LIMIT = 5


def trim_history_per_axis(history: list) -> list:
    """两轴各自保留最近 HISTORY_LIMIT 条，保持原有时间顺序。"""
    counts = {}
    kept = []
    for entry in reversed(history):
        axis = entry.get("axis")
        if counts.get(axis, 0) >= HISTORY_LIMIT:
            continue
        counts[axis] = counts.get(axis, 0) + 1
        kept.append(entry)
    return list(reversed(kept))


class FeedbackState:
    """决策侧双轴状态。旧 read_value/set_value/apply_delta 仅操作归属感。"""

    def __init__(self, persistence_dir: Optional[Path] = None):
        if persistence_dir is None:
            persistence_dir = data_root() / "brain" / "amy" / "persistence"
        persistence_dir.mkdir(parents=True, exist_ok=True)
        self.persistence_file = persistence_dir / "feedback_state.json"
        self._value = float(FEEDBACK_DEFAULT)
        self._competence_value = float(FEEDBACK_DEFAULT)
        self._history = []
        self._task_results = []
        self._applied_critical_actions = []
        self.load_from_file()

    def read_value(self) -> float:
        """读当前值（纯读，不落盘）。"""
        return self._value

    def set_value(self, value: float) -> None:
        """写入：clamp 到 [FEEDBACK_MIN, FEEDBACK_MAX] 后落账（不落盘，由门面 save）。"""
        if not math.isfinite(float(value)):
            raise ValueError("情感数值必须有限")
        self._value = clamp(float(value), float(FEEDBACK_MIN), float(FEEDBACK_MAX))

    def apply_delta(self, delta: float) -> float:
        """按格子步进结算一次：value += delta 后 clamp，返回结算后的值。

        结算即落账（内存），持久化由门面统一 save_to_file。
        """
        self.set_value(self._value + float(delta))
        return self._value

    def load_from_file(self) -> None:
        """从 feedback_state.json 载入 {value}；首启无文件保持默认（FEEDBACK_DEFAULT）。"""
        if not self.persistence_file.exists():
            return
        with open(self.persistence_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        value = float(data.get("value", FEEDBACK_DEFAULT))
        competence = float(data.get("competence_value", FEEDBACK_DEFAULT))
        if not math.isfinite(value) or not math.isfinite(competence):
            raise ValueError("情感数值必须有限")
        self._value = clamp(value, FEEDBACK_MIN, FEEDBACK_MAX)
        self._competence_value = clamp(competence, FEEDBACK_MIN, FEEDBACK_MAX)
        self._history = trim_history_per_axis(copy.deepcopy(data.get("history", [])))
        self._task_results = copy.deepcopy(data.get("task_results", [
            {"title": e["title"], "result": e["label"], "time": e["time"]}
            for e in self._history if e.get("axis") == "competence" and e.get("label") in (*TaskResultAct.names(), *TaskResultAct.legacy_results, "成功", "失败")
        ]))[-20:]
        self._applied_critical_actions = list(data.get("applied_critical_actions", []))

    def snapshot(self) -> dict:
        """两轴及各自最近五条变动的独立副本；旧 value 代表归属感。"""
        return copy.deepcopy({"schema_version": 2, "value": self._value,
                              "competence_value": self._competence_value, "history": self._history,
                              "task_results": self._task_results,
                              "applied_critical_actions": self._applied_critical_actions})

    def save_to_file(self) -> None:
        """同目录临时文件替换，失败上抛。"""
        fd, name = tempfile.mkstemp(dir=self.persistence_file.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.snapshot(), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.persistence_file)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def settle(self, title: str, changes: list, *, task_result: str = None) -> None:
        """一次收尾同时结算两轴；零增量不记，触顶/触底仍保留反馈原因。

        触底裁剪：某轴数值此前已被负分压到下限（FEEDBACK_MIN，即 0）后，
        再结算负增量时，追加本条负分记录后删除该轴最早的一条负分记录，
        使触底后的负分记录按 FIFO 替换而非无限堆积。
        数值仍按增量结算并 clamp，不因删除历史记录回涨。
        """
        previous = self.snapshot()
        try:
            for axis, label, delta, reason in changes:
                if axis not in ("competence", "recognition"):
                    raise ValueError(f"未知情感轴：{axis}")
                before = self._competence_value if axis == "competence" else self._value
                if not math.isfinite(float(delta)):
                    raise ValueError("情感增量必须有限")
                after = clamp(before + delta, FEEDBACK_MIN, FEEDBACK_MAX)
                if axis == "competence":
                    self._competence_value = after
                else:
                    self._value = after
                if delta != 0:
                    entry = {"axis": axis, "label": label, "delta": delta,
                             "before": before, "after": after, "title": title,
                             "reason": reason, "time": int(time.time())}
                    self._history.append(entry)
                    # 结算前该轴已触底，再遇负增量：删除该轴最早的负分记录
                    if delta < 0 and before <= FEEDBACK_MIN:
                        self._trim_oldest_negative(axis, entry)
            self._history = trim_history_per_axis(self._history)
            if task_result in TaskResultAct.names():
                self._task_results = (self._task_results + [{"title": title, "result": task_result, "time": int(time.time())}])[-20:]
            self.save_to_file()
        except Exception:
            self._value = previous["value"]
            self._competence_value = previous["competence_value"]
            self._history = previous["history"]
            self._task_results = previous["task_results"]
            raise

    def reset_for_critical_action(self, action_id: str, axis: str, title: str, reason: str) -> bool:
        """四种临界行动登记后重置对应轴；持久化编号防止恢复时重复重置。"""
        from config.maintenance_settings import RESET_VALUE
        if axis not in ("competence", "recognition") or not action_id or not reason.strip():
            raise ValueError("临界行动必须有有效编号、情感轴和原因")
        if action_id in self._applied_critical_actions:
            return False
        previous = self.snapshot()
        try:
            before = self._competence_value if axis == "competence" else self._value
            if axis == "competence":
                self._competence_value = float(RESET_VALUE)
            else:
                self._value = float(RESET_VALUE)
            self._history.append({"axis": axis, "label": "临界行动已登记", "delta": RESET_VALUE - before,
                                  "before": before, "after": RESET_VALUE, "title": title,
                                  "reason": reason, "time": int(time.time())})
            self._history = trim_history_per_axis(self._history)
            self._applied_critical_actions.append(action_id)
            self.save_to_file()
        except BaseException:
            self._value, self._competence_value = previous["value"], previous["competence_value"]
            self._history = previous["history"]
            self._applied_critical_actions = previous["applied_critical_actions"]
            raise
        return True

    def _trim_oldest_negative(self, axis: str, keep: dict) -> None:
        """删除指定轴最早的一条负分记录；不删刚追加的 keep 条目（无更早负分则不动作）。"""
        for index, entry in enumerate(self._history):
            if entry is keep:
                continue
            if entry["axis"] == axis and entry["delta"] < 0:
                del self._history[index]
                return


_feedback_state: Optional[FeedbackState] = None


def get_feedback_state(persistence_dir: Optional[Path] = None) -> FeedbackState:
    """获取决策情感状态单例（首次创建时载入持久化）。

    注意：persistence_dir 仅在首次创建时生效；换目录走 reset_feedback_state()
    后重建（web 预设 / 测试隔离流程依赖该语义）。
    """
    global _feedback_state
    if _feedback_state is None:
        _feedback_state = FeedbackState(persistence_dir)
    return _feedback_state


def reset_feedback_state() -> None:
    """清除单例（测试/预设环境用）。下次 get_feedback_state 重建。"""
    global _feedback_state
    _feedback_state = None
