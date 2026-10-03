#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 存在感常驻标量状态（原"反馈感"，2026-09-29 改名；内部标识符沿用 feedback_* 历史命名）

0-100 标量（默认 50，即"中性基线"），持久化到 feedback_state.json（文件名沿用历史）。
- 该量程**仅为方便**：实际是**格子式离散步进**，非连续平滑
  （无评价 -10 / 负面评价 -20 / 尚可 +10 / 优秀 +20，见 constants.FinalEvaluationAct.feedback_delta）。
- 无时间衰减：只在 apply_final_evaluation 单点结算（评价产生时按格子步进，clamp 到 [0,100]）。
- 结算即落账：apply_delta 更新 _value 后由门面统一 save_to_file。

与 AMY 原设计冲突，**暂以替代办法实现，随时可能推翻**（见 docs/未来改进.md 第一节 1.1）。
单例访问器：get_feedback_state() / reset_feedback_state()
（模块级工厂，普通类 + 全局 _instance；禁止类内 __new__ 单例模板），见 docs/api/amy.md。
"""

from paths import data_root
import json
from pathlib import Path
from typing import Optional

from brain.amy.constants import (
    clamp,
    FEEDBACK_DEFAULT, FEEDBACK_MIN, FEEDBACK_MAX,
)
from utils.error_handling import handle_errors
from utils.logger import get_logger

logger = get_logger('amy_feedback_state', 'INFO')


class FeedbackState:
    """存在感常驻标量状态（普通类，单例由模块级 get_feedback_state 持有）。

    内部状态：
    - _value: float  当前存在感数值（默认 FEEDBACK_DEFAULT=50）
    """

    def __init__(self, persistence_dir: Optional[Path] = None):
        if persistence_dir is None:
            persistence_dir = data_root() / "brain" / "amy" / "persistence"
        persistence_dir.mkdir(parents=True, exist_ok=True)
        self.persistence_file = persistence_dir / "feedback_state.json"
        self._value = float(FEEDBACK_DEFAULT)
        self.load_from_file()

    def read_value(self) -> float:
        """读当前值（纯读，不落盘）。"""
        return self._value

    def set_value(self, value: float) -> None:
        """写入：clamp 到 [FEEDBACK_MIN, FEEDBACK_MAX] 后落账（不落盘，由门面 save）。"""
        self._value = clamp(float(value), float(FEEDBACK_MIN), float(FEEDBACK_MAX))

    def apply_delta(self, delta: float) -> float:
        """按格子步进结算一次：value += delta 后 clamp，返回结算后的值。

        结算即落账（内存），持久化由门面统一 save_to_file。
        """
        self.set_value(self._value + float(delta))
        return self._value

    @handle_errors()
    def load_from_file(self) -> None:
        """从 feedback_state.json 载入 {value}；首启无文件保持默认（FEEDBACK_DEFAULT）。"""
        if not self.persistence_file.exists():
            return
        with open(self.persistence_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self._value = clamp(float(data.get("value", FEEDBACK_DEFAULT)), float(FEEDBACK_MIN), float(FEEDBACK_MAX))
        logger.info(f"Loaded feedback_state from persistence: value={self._value}")

    @handle_errors()
    def save_to_file(self) -> None:
        """持久化 {value} 到 feedback_state.json。"""
        data = {"value": self._value}
        with open(self.persistence_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


_feedback_state: Optional[FeedbackState] = None


def get_feedback_state(persistence_dir: Optional[Path] = None) -> FeedbackState:
    """获取存在感状态单例（首次创建时载入持久化）。

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
