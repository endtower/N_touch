# -*- coding: utf-8 -*-
"""情绪：从"拨数值"改成"认定发生了什么"。

主仓的 act 词表和效果表在 `brain/amy/constants.py`，那里是自包含的（只依赖
dataclasses/typing），所以这里直接按文件加载，不 import `brain.amy` 包
（避免触发它的 __init__）。

一次认定做三件事：
1. 查表拿到这个 act 的 ne_delta / da_delta
2. 写进 State（走命名事务，留账）
3. 返回当前情绪 + 描述，让她看得到自己落到哪一档
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional

_CONSTANTS = Path(__file__).resolve().parents[1] / "brain" / "amy" / "constants.py"

# 由系统注入、不由她认定的分类
_BLOCKED_CATEGORIES = {"system"}

# DA：动力/活力/行为方向
_DA_BANDS = [    (20, "提不起劲，什么都不想做"),
    (40, "有点闷，不太想动"),
    (60, "平稳，能做但不兴奋"),
    (80, "有精神，愿意往前推"),
    (100, "很亢奋，想立刻做点什么"),
]
# NE：戒备/攻击 vs 妥协/亲和
_NE_BANDS = [
    (20, "很软，什么都愿意让"),
    (40, "偏柔和，不太想争"),
    (60, "中性，不冷不热"),
    (80, "有点冲，说话带刺"),
    (100, "很戒备，随时准备顶回去"),
]


def _load_constants():
    if not _CONSTANTS.exists():
        return None
    spec = importlib.util.spec_from_file_location("_abyss_amy_constants", _CONSTANTS)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_constants()
ACTEFFECTS = getattr(_MOD, "ACTEffects", None)

# ============================================================
# 效果覆盖表（只在本内核生效，主仓 brain/amy/constants.py 不动）
#
# 主仓那张表是历史堆积出来的，跟"两轴"的语义有冲突：
#   - 被夸只降 NE，不涨 DA —— "开心"根本没被编码
#   - 冷静下来降的是 DA(动力)，不是 NE(锋利) —— 冷却变成没劲
#   - 主动嘲讽 NE -10 —— 开火反而更不冲，方向反了
#
# 这里按两轴重新对齐：
#   NE = 语言锋利度 / 生气 / 戒备（越高越冲，越低越软）
#   DA = 动力 / 开心（越高越有劲越想动手，越低越蔫）
#
# 三条规则：
#   1) 别人的行为：越伤人的越涨 NE、越掉 DA；越暖的越降 NE、越涨 DA
#   2) 自己的成败：只动 DA，不动 NE（成败不该让人变凶）
#   3) 自我调节：冷静压火(NE↓)不压劲，共情变软(NE↓)，提气加劲(DA↑)
# ============================================================
_OVERRIDES: Dict[str, Dict[str, int]] = {
    # —— 别人对我：伤人 ——
    "被攻击/被羞辱/被挑衅": {"da": -10, "ne": +20},
    "被质疑/被挑剔": {"da": -5, "ne": +8},
    "被冷漠对待/消息被晾置": {"da": -8, "ne": +5},
    # —— 别人对我：暖 ——
    "被示好/被亲近": {"da": +8, "ne": -8},
    "被夸奖/被善意支持/被希望": {"da": +12, "ne": -15},
    "被真诚道歉/对方妥协安抚/被祈求": {"da": +5, "ne": -22},
    # —— 我做得怎么样：只动 DA ——
    "策略卓越/超预期达成": {"da": +25, "ne": 0},
    "策略有效/推进顺利": {"da": +15, "ne": 0},
    "收获新启发/有价值经验": {"da": +8, "ne": 0},
    "表现平淡/未达预期": {"da": -6, "ne": 0},
    "策略受挫/陷入被动": {"da": -15, "ne": +3},
    "严重失败/全盘搞砸": {"da": -25, "ne": +5},
    # —— 我的做法 ——
    "主动嘲讽/开火回怼": {"da": +5, "ne": +12},
    "主动退让/低头道歉": {"da": -5, "ne": -10},
    # —— 自我调节（保底） ——
    "冷静下来": {"da": -5, "ne": -20},
    "共情对方": {"da": 0, "ne": -25},
    "提起劲头": {"da": +25, "ne": -3},
}


def _band(value: int, bands) -> str:
    for upper, text in bands:
        if value <= upper:
            return text
    return bands[-1][1]


def da_desc(value: int) -> str:
    return f"{value}/100，{_band(value, _DA_BANDS)}"


def ne_desc(value: int) -> str:
    return f"{value}/100，{_band(value, _NE_BANDS)}"


def available_acts() -> List[str]:
    """她能认定的行为清单（去掉系统内置类）。"""
    if ACTEFFECTS is None:
        return []
    return [
        name
        for name in ACTEFFECTS.all().keys()
        if ACTEFFECTS.category_of(name) not in _BLOCKED_CATEGORIES
    ]


def describe_act(act: str) -> Optional[Dict[str, Any]]:
    if ACTEFFECTS is None:
        return None
    entry = ACTEFFECTS.lookup(act)
    if entry is None:
        return None
    ov = _OVERRIDES.get(act)
    da = ov["da"] if ov else int(entry.get("da_delta", 0) or 0)
    ne = ov["ne"] if ov else int(entry.get("ne_delta", 0) or 0)
    return {
        "name": act,
        "category": ACTEFFECTS.category_of(act),
        "da_delta": da,
        "ne_delta": ne,
        "desc": entry.get("desc", "") or "",
        "source": "override" if ov else "main",
    }


def feel(state, log, act: str) -> str:
    """认定一次「发生了某事」，落到情绪上，并把当前状态返回给调用方。

    Raises:
        ValueError: act 不在词表 / 属系统内置。
    """
    info = describe_act(act)
    if info is None:
        raise ValueError(f"不认识的认定：{act}")
    if info["category"] in _BLOCKED_CATEGORIES:
        raise ValueError(f"「{act}」是系统内置行为，不能由你自己认定")
    if act == getattr(state, "last_feel", ""):
        raise ValueError(
            f"「{act}」你刚认定过了。同一件事连拍重复认定会把情绪打满，"
            "换成别的说法或者就让它停在这一拍。"
        )
    state.last_feel = act

    changes = {}
    if info["da_delta"]:
        changes["mood_da"] = info["da_delta"]
    if info["ne_delta"]:
        changes["mood_ne"] = info["ne_delta"]
    if changes:
        state.transition(log, "feel", act, **changes)

    delta_bits = []
    if info["da_delta"]:
        delta_bits.append(f"DA {info['da_delta']:+d}")
    if info["ne_delta"]:
        delta_bits.append(f"NE {info['ne_delta']:+d}")
    delta_text = "，".join(delta_bits) if delta_bits else "无变化"

    return (
        f"已认定：{act}（{info['category']}），本拍 {delta_text}。\n"
        f"【当前情绪】\n"
        f"  DA {da_desc(state.mood['da'])}\n"
        f"  NE {ne_desc(state.mood['ne'])}"
    )
