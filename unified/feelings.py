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
_DA_BANDS = [
    (20, "提不起劲，什么都不想做"),
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
    return {
        "name": act,
        "category": ACTEFFECTS.category_of(act),
        "da_delta": int(entry.get("da_delta", 0) or 0),
        "ne_delta": int(entry.get("ne_delta", 0) or 0),
        "desc": entry.get("desc", "") or "",
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
