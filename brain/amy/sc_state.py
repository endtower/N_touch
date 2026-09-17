#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy SC（自我一致性/认知自信轴）常驻标量状态

0-100 标量（默认 40，即"平淡"基线），持久化到 sc_state.json：
- 惰性时间衰减：读取时按距上次结算的小时数朝 SC_DECAY_TARGET(=40，与 SC_DEFAULT
  一致的基线) 双向收敛（SC_DECAY_PER_HOUR=2 点/小时，低于基线回升、高于基线回落），
  公式走 smoothing.decay_toward
- 结算即落账：read_value / apply_decay 在返回计算结果的同时更新 _value 与
  _last_calc_time 为当前时刻，避免重复结算
- 无评价抚平（pull_toward_interval）不在此实现，由门面计算后 set_value 写入，
  本模块只负责"常驻标量 + 惰性衰减 + 持久化"
- 持久化由 save_to_file / 门面统一处理；read_value 只返回 float 不落盘

单例访问器：get_sc_state() / reset_sc_state()（模块级工厂，普通类 + 全局 _instance，
禁止类内 __new__ 单例样板），见 docs/api/amy.md。
"""

import json
import time
from pathlib import Path
from typing import Optional

from brain.amy.constants import (
    clamp,
    SC_DEFAULT, SC_MIN, SC_MAX,
    SC_DECAY_PER_HOUR, SC_DECAY_TARGET,
)
from brain.amy.smoothing import decay_toward
from utils.error_handling import handle_errors
from utils.logger import get_logger

logger = get_logger('amy_sc_state', 'INFO')


class ScState:
    """SC 常驻标量状态（普通类，单例由模块级 get_sc_state 持有）。

    内部状态：
    - _value: float           当前 SC 值（默认 SC_DEFAULT=40）
    - _last_calc_time: float  上次结算时刻（epoch 秒）
    """

    def __init__(self, persistence_dir: Optional[Path] = None):
        if persistence_dir is None:
            persistence_dir = Path(__file__).resolve().parent / "persistence"
        persistence_dir.mkdir(exist_ok=True)
        self.persistence_file = persistence_dir / "sc_state.json"
        self._value = float(SC_DEFAULT)
        self._last_calc_time = float(time.time())
        self.load_from_file()

    def _settle(self) -> float:
        """结算一次惰性时间衰减（结算即落账，更新 _value 与 _last_calc_time）。

        按 time.time() - _last_calc_time 换算小时 dt，调 decay_toward 得到衰减后值；
        返回结算后的 _value。
        """
        now = time.time()
        dt_hours = (now - self._last_calc_time) / 3600.0
        if dt_hours <= 0:
            return self._value
        self._value = decay_toward(self._value, SC_DECAY_TARGET, SC_DECAY_PER_HOUR, dt_hours)
        self._last_calc_time = now
        return self._value

    def read_value(self) -> float:
        """读数：先做一次惰性时间衰减结算，再返回结算后的值。

        只返回 float，不持久化（持久化由 save_to_file / 门面统一处理）。
        """
        return self._settle()

    def set_value(self, value: float) -> None:
        """写入：clamp 到 [SC_MIN, SC_MAX] 后落账，并刷新 _last_calc_time。"""
        self._value = clamp(float(value), float(SC_MIN), float(SC_MAX))
        self._last_calc_time = time.time()

    def apply_decay(self) -> float:
        """显式触发一次惰性时间衰减结算（供测试/门面用），返回结算后的值。"""
        return self._settle()

    @handle_errors()
    def load_from_file(self) -> None:
        """从 sc_state.json 载入 {value, last_calc_time}；首启无文件保持默认（SC_DEFAULT）。"""
        if not self.persistence_file.exists():
            return
        with open(self.persistence_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self._value = clamp(float(data.get("value", SC_DEFAULT)), float(SC_MIN), float(SC_MAX))
        self._last_calc_time = float(data.get("last_calc_time", time.time()))
        logger.info(f"Loaded sc_state from persistence: value={self._value}")

    @handle_errors()
    def save_to_file(self) -> None:
        """持久化 {value, last_calc_time} 到 sc_state.json；保存前自动先做一次惰性结算。"""
        self._settle()
        data = {"value": self._value, "last_calc_time": self._last_calc_time}
        with open(self.persistence_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


_sc_state: Optional[ScState] = None


def get_sc_state(persistence_dir: Optional[Path] = None) -> ScState:
    """获取 SC 状态单例（首次创建时载入持久化）。

    注意：persistence_dir 仅在首次创建时生效；换目录走 reset_sc_state()
    后重建（web 预设 / 测试隔离流程依赖该语义）。
    """
    global _sc_state
    if _sc_state is None:
        _sc_state = ScState(persistence_dir)
    return _sc_state


def reset_sc_state() -> None:
    """清除单例（测试/预设环境用）。下次 get_sc_state 重建。"""
    global _sc_state
    _sc_state = None