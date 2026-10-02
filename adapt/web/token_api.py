# -*- coding: utf-8 -*-
"""
adapt.web.token_api
===================

Token 用量统计查看接口（只读）.

对前端暴露按天 / agent / API 提供商（家）/ 模型 的 LLM 与嵌入调用用量:
  - totals     : 查询区间内合计（token / 缓存命中 / 花费）
  - per_day    : 按天明细（每天各自的 by_agent / by_provider / by_model）
  - by_agent   : 区间内按 agent 聚合
  - by_provider: 区间内按 API 提供商聚合
  - by_model   : 区间内按模型聚合

数据源: utils/llm_client_manager 的 token_stats.json（v2 按天分桶 schema）.

端点:
  GET /token-stats?start=YYYY-MM-DD&end=YYYY-MM-DD
    - 缺省区间: 最近 30 天（含今天）

只读, 不写.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple

from aiohttp import web

from utils.logger import get_logger
from utils.llm_client_manager import load_stats, empty_bucket

logger = get_logger(__name__, "DEBUG")

# 参与累加的数值字段（models / providers 列表另作合并）
_SUM_KEYS = (
    "call_count", "prompt_tokens", "completion_tokens", "total_tokens",
    "cached_tokens", "cache_hit_tokens", "cache_miss_tokens", "reasoning_tokens",
    "cost_cache_miss", "cost_cache_hit", "cost_output", "cost_total",
)


def _parse_date(value: str) -> str | None:
    """解析 YYYY-MM-DD；空串返回 None，格式非法抛 ValueError。"""
    text = (value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"日期格式应为 YYYY-MM-DD: {text}")


def _resolve_range(request: web.Request) -> Tuple[str, str]:
    """解析查询区间，缺省为最近 30 天（含今天）。"""
    end = _parse_date(request.query.get("end", "")) or datetime.now().strftime("%Y-%m-%d")
    start = _parse_date(request.query.get("start", ""))
    if start is None:
        start = (datetime.strptime(end, "%Y-%m-%d") - timedelta(days=29)).strftime("%Y-%m-%d")
    if start > end:
        raise ValueError(f"start({start}) 晚于 end({end})")
    return start, end


def _merge_into(target: Dict[str, Any], bucket: Dict[str, Any]) -> None:
    """把单个分桶累加进汇总桶（数值相加，models/providers 取并集）。"""
    for key in _SUM_KEYS:
        target[key] = round(target[key] + (bucket.get(key) or 0), 8)
    for model in bucket.get("models", []) or []:
        if model not in target["models"]:
            target["models"].append(model)
    for provider in bucket.get("providers", []) or []:
        if provider not in target["providers"]:
            target["providers"].append(provider)


def _aggregate(days: Dict[str, Any], day_keys: List[str]) -> Dict[str, Any]:
    """按天聚合区间数据：区间合计 + 各维度合计 + 按天明细。"""
    totals = empty_bucket()
    by_agent: Dict[str, Any] = {}
    by_provider: Dict[str, Any] = {}
    by_model: Dict[str, Any] = {}
    per_day: List[Dict[str, Any]] = []

    for day in day_keys:
        entry = days.get(day) or {}
        agent_map = entry.get("by_agent") or {}
        provider_map = entry.get("by_provider") or {}
        model_map = entry.get("by_model") or {}

        # 单日合计只取 by_agent（每次调用在 by_agent 中恰好计一次，避免跨维度重复累加）
        day_totals = empty_bucket()
        for key, bucket in agent_map.items():
            _merge_into(day_totals, bucket)
            _merge_into(by_agent.setdefault(key, empty_bucket()), bucket)
        for key, bucket in provider_map.items():
            _merge_into(by_provider.setdefault(key, empty_bucket()), bucket)
        for key, bucket in model_map.items():
            _merge_into(by_model.setdefault(key, empty_bucket()), bucket)

        _merge_into(totals, day_totals)
        per_day.append({
            "date": day,
            "totals": day_totals,
            "by_agent": agent_map,
            "by_provider": provider_map,
            "by_model": model_map,
        })

    return {
        "totals": totals,
        "per_day": per_day,
        "by_agent": by_agent,
        "by_provider": by_provider,
        "by_model": by_model,
    }


async def handle_token_stats(request: web.Request) -> web.Response:
    """GET /token-stats  返回区间内的 token 用量统计（只读）。"""
    try:
        start, end = _resolve_range(request)
        data = load_stats()
        days = data.get("days", {}) or {}
        day_keys = sorted(k for k in days.keys() if start <= k <= end)
        result = _aggregate(days, day_keys)
        result["range"] = {"start": start, "end": end, "days": day_keys}
        result["updated_at"] = data.get("updated_at")
        result["fetched_at"] = time.time()
        return web.json_response(result)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:
        logger.exception("[/token-stats] 失败")
        return web.json_response({"error": f"读取 token 统计失败: {e}"}, status=500)