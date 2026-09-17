# -*- coding: utf-8 -*-
"""
adapt.web.amy_api
=================

Amy 情绪系统状态查看与想法池调试编辑接口.

对前端暴露 LLM 当前情绪的完整快照:
  - 去甲肾上腺素 (NE) / 多巴胺 (DA) / SC 当前值 (clamp 后)
  - 所有 active thought (含 act / intensity / expire_time / desc)
  - 触发当前 prompt 的统一描述 (get_amy_description 输出)
  - 全部 ACT 表 (按分类组织, 给前端展示) + SC 评价词表 (final)
  - SC 轴元信息 (sc_meta: 衰减/抚平/评价效果参数)
同时提供想法池调试编辑 (增/删/改), 供前端测试:
  - POST /amy/thoughts      新建 thought
  - PATCH /amy/thoughts/{id} 修改 thought
  - DELETE /amy/thoughts/{id} 删除 thought
  - POST /amy/final-evaluation  最终评价完整闭环（SC 涨跌 + 想法入池）

注：崩溃/覆盖机制已于 2026-08-30 移除，响应中的 system_state 块已删除。

端点:
  GET /amy/state          -> 一次性返回上面所有信息
  POST/PATCH/DELETE /amy/thoughts... -> 想法池调试 CRUD
  POST /amy/final-evaluation       -> 最终评价登记（SC + 想法入池）
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
        from brain.amy.constants import ACTEffects, FinalEvaluationAct
        if act in FinalEvaluationAct.names():
            return "final"
        c = ACTEffects.category_of(act)
        return c or "unknown"
    except Exception:
        return "unknown"


def _serialize_acts() -> Dict[str, List[Dict[str, Any]]]:
    """导出全部 ACT (按分类)。"""
    from brain.amy.constants import (
        ExternalInteractionAct, EvaluationAct,
        SelfActionAct, SystemBuiltInAct, FinalEvaluationAct,
        SC_EFFECT_GOOD, SC_EFFECT_BAD,
    )
    from brain.amy.smoothing import (
        SMOOTH_TARGET, SMOOTH_STEP,
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
    # 最终评价词表（PFCA 收尾）：纯词表，不参与 thought 体系
    def _to_final_list() -> List[Dict[str, Any]]:
        out = []
        for item in FinalEvaluationAct.items:
            if item["sc_kind"] == "good":
                effect = f"SC {SC_EFFECT_GOOD:+g}"
            elif item["sc_kind"] == "bad":
                effect = f"SC {SC_EFFECT_BAD:+g}"
            else:
                effect = f"向 {SMOOTH_TARGET} 抚平 {SMOOTH_STEP}"
            out.append({
                "name": item["name"],
                "sc_kind": item["sc_kind"],
                "effect": effect,
            })
        return out
    return {
        "external": _to_list(ExternalInteractionAct),
        "evaluation": _to_list(EvaluationAct),
        "self_action": _to_list(SelfActionAct),
        "system": _to_list(SystemBuiltInAct),
        "final": _to_final_list(),
    }


def _serialize_amy_descriptions() -> Dict[str, Any]:
    from brain.amy.constants import (
        DOPAMINE_DESCRIPTIONS,
        NOREPINEPHRINE_DESCRIPTIONS,
        MIN_VALUE, MAX_VALUE,
        SC_MIN, SC_MAX, SC_DEFAULT,
        SC_ACT_BANDS,
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
        # SC 描述带 = A 组行动对应表单条（每 10 分一档，不再整段 dump 描述带+行动指令）
        "sc": [
            {"lo": lo, "hi": hi - 0.001 if i < len(SC_ACT_BANDS) - 1 else hi, "text": text}
            for i, (lo, hi, text) in enumerate(SC_ACT_BANDS)
        ],
        "range": {
            "min": MIN_VALUE,
            "max": MAX_VALUE,
            "sc_min": SC_MIN,
            "sc_max": SC_MAX,
        },
    }


# ============================================================
# handler
# ============================================================

def _sc_meta() -> Dict[str, Any]:
    """SC 轴的展示元信息（衰减/抚平/评价效果参数），供前端展示规则。"""
    from brain.amy.constants import (
        SC_DEFAULT, SC_MIN, SC_MAX,
        SC_DECAY_PER_HOUR, SC_DECAY_TARGET,
        SC_EFFECT_GOOD, SC_EFFECT_BAD,
    )
    from brain.amy.smoothing import (
        SMOOTH_TARGET, SMOOTH_STEP,
    )
    return {
        "default": SC_DEFAULT,
        "min": SC_MIN,
        "max": SC_MAX,
        "decay_per_hour": SC_DECAY_PER_HOUR,
        "decay_target": SC_DECAY_TARGET,
        "effect_good": SC_EFFECT_GOOD,
        "effect_bad": SC_EFFECT_BAD,
        "smooth_target": SMOOTH_TARGET,
        "smooth_step": SMOOTH_STEP,
    }

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
                "sc": round(snapshot.sc, 2),
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
            "sc_meta": _sc_meta(),
        })
    except Exception as e:
        logger.exception("[/amy/state] 失败")
        return web.json_response({"error": f"获取 Amy 状态失败: {e}"}, status=500)


# ============================================================
# 想法池调试编辑（增/删/改，供前端测试）
# ============================================================

async def handle_create_amy_thought(request: web.Request) -> web.Response:
    """POST /amy/thoughts  新建一条 thought（调试/测试用）。

    body: {act, desc, intensity?, duration?}
    - act：ACT 表内的 act 名（system 类拒绝）
    - duration（秒）缺省用 THOUGHT_DURATION
    """
    ei = await _try_get_ei()
    if ei is None:
        return web.json_response({"initialized": False, "error": "EmotionalInterface 未初始化"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    act = (data.get("act") or "").strip()
    desc = (data.get("desc") or "").strip()
    if not act or not desc:
        return web.json_response({"error": "act 与 desc 必填"}, status=400)
    try:
        intensity = float(data.get("intensity", 1.0))
        duration = int(data["duration"]) if data.get("duration") not in (None, "") else None
    except (TypeError, ValueError):
        return web.json_response({"error": "intensity/duration 必须是数字"}, status=400)
    try:
        thought = await ei.thought_cache.create_thought_debug(
            act=act, desc=desc, intensity=intensity, duration=duration,
        )
        ei.thought_cache.save_to_file()
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"created": _serialize_thoughts([thought])[0]}, status=201)


async def handle_apply_amy_final_evaluation(request: web.Request) -> web.Response:
    """POST /amy/final-evaluation  登记一次最终评价（调试/测试用，完整闭环）。

    body: {final_evaluation, title}
    - final_evaluation：FinalEvaluationAct 词表名
    - title：本次事件简易标题（≤15 字）

    直接走 `apply_final_evaluation`：SC 涨跌（good +10 / bad -10 / none 朝 40 单点抚平）
    + 想法入池（desc=`在事件{title} 中，{评价词}`，时长 4 小时）。
    与前端的创建想法（POST /amy/thoughts，纯想法池、不动 SC）区分，这里是完整闭环。
    失败（未初始化 / 词表外 / title 空）返回 400，不吞异常。
    """
    ei = await _try_get_ei()
    if ei is None:
        return web.json_response({"initialized": False, "error": "EmotionalInterface 未初始化"}, status=503)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    final_evaluation = (data.get("final_evaluation") or "").strip()
    title = (data.get("title") or "").strip()
    try:
        await ei.apply_final_evaluation(final_evaluation, title)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    return web.json_response({"applied": True, "sc": ei.sc_state.read_value()})


async def handle_update_amy_thought(request: web.Request) -> web.Response:
    """PATCH /amy/thoughts/{thought_id}  修改一条 thought（调试/测试用）。

    body 可选字段：{desc?, act?, intensity?, duration?}
    - duration（秒）填了则把剩余时长重置为 now + duration；留空保持原 expire_time
    """
    ei = await _try_get_ei()
    if ei is None:
        return web.json_response({"initialized": False, "error": "EmotionalInterface 未初始化"}, status=503)
    thought_id = request.match_info.get("thought_id", "")
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON"}, status=400)
    desc = data.get("desc")
    act = data.get("act")
    intensity_raw = data.get("intensity")
    try:
        intensity = float(intensity_raw) if intensity_raw not in (None, "") else None
        expire_time = None
        if data.get("duration") not in (None, ""):
            expire_time = int(time.time()) + int(data["duration"])
    except (TypeError, ValueError):
        return web.json_response({"error": "intensity/duration 必须是数字"}, status=400)
    try:
        thought = await ei.thought_cache.update_thought_debug(
            thought_id,
            desc=desc if desc not in (None, "") else None,
            act=act if act not in (None, "") else None,
            intensity=intensity,
            expire_time=expire_time,
        )
        ei.thought_cache.save_to_file()
    except ValueError as e:
        status = 404 if "不存在" in str(e) else 400
        return web.json_response({"error": str(e)}, status=status)
    return web.json_response({"updated": _serialize_thoughts([thought])[0]})


async def handle_delete_amy_thought(request: web.Request) -> web.Response:
    """DELETE /amy/thoughts/{thought_id}  删除一条 thought（调试/测试用）。

    system 类 thought 删除后会在下次启动 bootstrap 恢复。
    """
    ei = await _try_get_ei()
    if ei is None:
        return web.json_response({"initialized": False, "error": "EmotionalInterface 未初始化"}, status=503)
    thought_id = request.match_info.get("thought_id", "")
    try:
        await ei.thought_cache.delete_thought(thought_id)
        ei.thought_cache.save_to_file()
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.json_response({"deleted": thought_id})