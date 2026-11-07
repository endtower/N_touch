#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Amy 情绪系统数值平滑原语

无副作用的纯函数集合：不持有状态、不依赖外部服务、不触碰单例，
供情绪轴做数值收敛（单点/区间抚平）与时间衰减复用。
"""


def pull_toward_value(value: float, target: float, step: float) -> float:
    """把数值朝 target 单点拉近 step 距离。

    规则：
    - value == target：返回原值（已达目标，不做任何修改）
    - value < target：返回 min(target, value + step)（最多拉到 target，不过冲）
    - value > target：返回 max(target, value - step)（最多降到 target，不过冲）
    - step <= 0：视为无操作，返回原值
    """
    if step <= 0 or value == target:
        return value
    if value < target:
        return min(target, value + step)
    return max(target, value - step)


def pull_toward_interval(value: float, low: float, high: float, step: float) -> float:
    """把数值朝闭区间 [low, high] 拉近 step 距离。

    通用区间原语（供其他场景复用）。

    规则：
    - value 已在区间内：返回原值（不做任何修改）
    - value < low：返回 min(low, value + step)（最多拉到 low，不过冲）
    - value > high：返回 max(high, value - step)（最多降到 high，不过冲）
    - step <= 0：视为无操作，返回原值
    """
    if step <= 0:
        return value
    if value < low:
        return min(low, value + step)
    if value > high:
        return max(high, value - step)
    return value


def decay_toward(value: float, target: float, rate_per_hour: float, dt_hours: float) -> float:
    """按时间差朝 target 衰减（速率 * 时间 的线性近似，不指数化）。

    公式：value - sign(value - target) * min(abs(value - target), rate_per_hour * dt_hours)

    规则：
    - value == target：返回原值（无差距可衰减）
    - dt_hours <= 0：视为无操作，返回原值
    - 结果向 target 收敛但不过冲：衰减量取"剩余差距"与"速率 * 时间"的较小者
    """
    if value == target or dt_hours <= 0:
        return value
    amount = min(abs(value - target), rate_per_hour * dt_hours)
    if value > target:
        return value - amount
    return value + amount