# -*- coding: utf-8 -*-
"""
adapt.web.amy_api
=================

Amy 情绪系统状态查看接口 (只读).

对前端暴露 LLM 当前情绪的完整快照:
  - 去甲肾上腺素 (NE) / 多巴胺 (DA) 当前值 (clamp 后)
  - 所有 active thought (含 act / intensity / expire_time / desc)
  - 触发当前 prompt 的统一描述 (get_amy_description 输出)
  - 全部 ACT 表 (按分类组织, 给前端展示)

注：崩溃/覆盖机制已于 2026-08-30 移除，响应中的 system_state 块已删除。

端点:
  GET /amy/state          -> 一次性返回上面所有信息
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from aiohttp import web

from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


async def _try_get_ei():
    """安全拿单例; 系统未启动时返回 None."""
    try:
        from brain.amy.emotional_interface import get_emotional_interface
        return await get_emotional_interface()
    except Exception as e:
        logger.warning(f"EmotionalInterface 尚未初始化: {e}")
        return None


def _serialize_thoughts(thoughts: List[Any]) -> List[Dict[str, Any]]:
    now = time.time()
    out: List[Dict[str, Any]] = []
    for t in thoughts:
        try:
            remaining = max(0, int(t.expire_time - now))
            out.append({
                "id": t.id,
                "act": t.act,
                "desc": t.desc,
                "intensity": float(getattr(t, "intensity", 1.0)),
                "create_time": int(t.create_time),
                "expire_time": int(t.expire_time),
                "remaining_seconds": remaining,
                "category": _category_of(t.act),
            })
        except Exception:
            continue
    # 倒序: 最新在前
    out.sort(key=lambda x: x["create_time"], reverse=True)
    return out


def _category_of(act: str) -> str:
    try:
        from brain.amy.constants import ACTEffects
        c = ACTEffects.category_of(act)
        return c or "unknown"
    except Exception:
        return "unknown"


def _serialize_acts() -> Dict[str, List[Dict[str, Any]]]:
    """导出全部 ACT (按分类)."""
    from brain.amy.constants import (
        ExternalInteractionAct, EvaluationAct,
        SelfActionAct, SystemBuiltInAct,
    )
    def _to_list(cls) -> List[Dict[str, Any]]:
        return [
            {
                "name": item["name"],
                "ne_delta": item.get("ne_delta", 0),
                "da_delta": item.get("da_delta", 0),
                "max_stack": item.get("max_stack", 1),
                "stack_coefficient": item.get("stack_coefficient", 0),
                "duration": item.get("duration", 0),
                "system_id": item.get("system_id"),
            }
            for item in cls.entries()
        ]
    return {
        "external": _to_list(ExternalInteractionAct),
        "evaluation": _to_list(EvaluationAct),
        "self_action": _to_list(SelfActionAct),
        "system": _to_list(SystemBuiltInAct),
    }


def _serialize_amy_descriptions() -> Dict[str, Any]:
    from brain.amy.constants import (
        DOPAMINE_DESCRIPTIONS,
        NOREPINEPHRINE_DESCRIPTIONS,
        MIN_VALUE, MAX_VALUE,
    )
    return {
        "dopamine": [
            {"lo": lo, "hi": hi, "text": text}
            for (lo, hi, text) in DOPAMINE_DESCRIPTIONS
        ],
        "norepinephrine": [
            {"lo": lo, "hi": hi, "text": text}
            for (lo, hi, text) in NOREPINEPHRINE_DESCRIPTIONS
        ],
        "range": {"min": MIN_VALUE, "max": MAX_VALUE},
    }


# ============================================================
# handler
# ============================================================

async def handle_get_amy_state(request: web.Request) -> web.Response:
    ei = await _try_get_ei()
    if ei is None:
        return web.json_response({
            "initialized": False,
            "error": "EmotionalInterface 未初始化 (system_core 还没就绪?)",
        }, status=503)

    try:
        # 1. 统一快照出口
        snapshot = await ei.get_mood_snapshot()

        # 2. amy 统一描述 (LLM 实际看到的)
        try:
            amy_desc = await ei.get_amy_description()
        except Exception as e:
            logger.warning(f"get_amy_description 失败: {e}")
            amy_desc = ""

        # 3. top thoughts (从 snapshot 取) + 全部 active
        top_thoughts = _serialize_thoughts(snapshot.top_thoughts or [])
        all_active = _serialize_thoughts(await ei.thought_cache.get_active_thoughts())

        # 4. thought_cache 统计
        ts = ei.thought_cache.timestamp

        return web.json_response({
            "initialized": True,
            "fetched_at": time.time(),
            "mood": {
                "norepinephrine": round(snapshot.norepinephrine, 2),
                "dopamine": round(snapshot.dopamine, 2),
            },
            "amy_description": amy_desc,
            "thoughts": {
                "all_active_count": len(all_active),
                "all_active": all_active,
                "top": top_thoughts,
            },
            "thought_cache_timestamp": ts,
            "acts": _serialize_acts(),
            "amy_descriptions": _serialize_amy_descriptions(),
        })
    except Exception as e:
        logger.exception("[/amy/state] 失败")
        return web.json_response({"error": f"获取 Amy 状态失败: {e}"}, status=500)