# -*- coding: utf-8 -*-
"""Truck 启动配置与运行时控制。

管理服务统一保存启动配置；运行时切换通过 station 应用并返回真实状态。
旧 WS 切换只影响当前进程，下次启动仍使用已保存的启动配置。
"""

from typing import Dict, Any, List

from utils.logger import setup_logging

logger = setup_logging(__name__, log_level="INFO")


# truck 描述表 (给前端展示)
from truck_settings_store import TRUCKS, read_settings, snapshot

_TRUCK_META = {
    tid: {"label": meta[0], "description": meta[1], "category": meta[2]}
    for tid, meta in TRUCKS.items()
}

def _apply_to_config(truck_id: str, enabled: bool) -> None:
    """把单个 truck 的启用状态直接写到 truck_settings.py 的 config 实例上 (内存生效, 不持久化)"""
    try:
        from config.truck_settings import (
            private_truck_config, ongoing_truck_config,
            special_attention_truck_config, scheduled_proactive_truck_config,
            reflection_char_truck_config, reflection_time_truck_config,
            reflection_efficacy_truck_config, reflection_belonging_truck_config,
            task_truck_config,
        )
        mapping = {
            "private_truck": private_truck_config,
            "ongoing_truck": ongoing_truck_config,
            "special_attention_truck": special_attention_truck_config,
            "scheduled_proactive_truck": scheduled_proactive_truck_config,
            "reflection_char_truck": reflection_char_truck_config,
            "reflection_time_truck": reflection_time_truck_config,
            "reflection_efficacy_truck": reflection_efficacy_truck_config,
            "reflection_belonging_truck": reflection_belonging_truck_config,
            "task_truck": task_truck_config,
        }
        cfg = mapping.get(truck_id)
        if cfg is not None:
            cfg.ENABLED = bool(enabled)
    except Exception as e:
        logger.warning(f"应用 truck {truck_id} 状态覆盖失败: {e}")


def init_from_disk() -> None:
    """兼容旧调用；启动配置已由 SystemCore 在 station 启动前应用。"""
    return


def get_state_snapshot() -> List[Dict[str, Any]]:
    """获取所有 truck 的当前状态快照 (给前端)

    enabled 字段以 AttentionStation 的实际运行状态为准 (而不是 config 文件),
    这样前端展示的状态是真实的, 不会出现 "显示启用但实际没跑" 的错觉.
    """
    try:
        from config.truck_settings import get_all_truck_configs
        configs = get_all_truck_configs()
    except Exception:
        configs = {}

    # 尝试从 station 拿真实运行状态
    real_states: Dict[str, str] = {}
    try:
        from brain.att.station import get_truck_state
        for tid in _TRUCK_META:
            real_states[tid] = get_truck_state(tid)
    except Exception:
        pass

    settings = read_settings()
    result = []
    for tid, meta in _TRUCK_META.items():
        cfg = configs.get(tid)
        # 真实运行状态: 如果 station 报告 running/paused, 视为启用
        real = real_states.get(tid, "unknown")
        if real in ("running", "paused"):
            actually_enabled = True
        elif real == "stopped":
            actually_enabled = False
        else:
            actually_enabled = False
        result.append({
            "truck_id": tid,
            "label": meta["label"],
            "description": meta["description"],
            "category": meta["category"],
            "enabled": actually_enabled,
            "configured_enabled": settings["enabled"][tid],
            "runtime_state": real,
            "cycle_time": (
                getattr(cfg, "CYCLE_TIME", None)
                or getattr(cfg, "CHECK_CYCLE_TIME", None)
            ) if cfg else None,
        })
    return result


def set_truck_enabled(truck_id: str, enabled: bool) -> Dict[str, Any]:
    """设置 truck 启用状态, 返回更新后的快照

    注意: 此覆盖仅在当前进程内有效，后端重启后使用已保存的启动配置。

    Args:
        truck_id: truck 标识
        enabled: True 启用 / False 停用

    Returns:
        dict: {"ok": bool, "error": str?, "snapshot": {...}, "all": [...]}
    """
    truck_id = truck_id.lower()
    if truck_id not in _TRUCK_META:
        return {"ok": False, "error": f"未知 truck: {truck_id}"}

    _apply_to_config(truck_id, enabled)

    # 关键: 通知 AttentionStation 真正启停 truck 实例
    # (只改 config 是不够的, station.start() 只在启动时检查 config)
    try:
        from brain.att.station import set_truck_enabled as _station_set
        ok = _station_set(truck_id, enabled)
        if not ok:
            logger.warning(f"station 未运行或未知 truck: {truck_id}")
    except Exception as e:
        logger.warning(f"运行时切换 truck 失败: {e}")

    snapshot = next((t for t in get_state_snapshot() if t["truck_id"] == truck_id), None)
    logger.info(f"truck {truck_id} 已{'启用' if enabled else '停用'}")
    return {"ok": True, "snapshot": snapshot, "all": get_state_snapshot()}


async def handle_console_status(request):
    from aiohttp import web
    return web.json_response({"manager_available": False, "core_ready": True})


async def handle_console_trucks(request):
    from aiohttp import web
    from .system_api import _authorized, _denied
    if not _authorized(request):
        return _denied()
    if request.method == "PUT":
        # 配置写入由常驻管理服务单独持有，避免 Core/GUI 并发覆盖同一文件。
        import aiohttp
        from .system_api import _SERVICE_BASE, _manager_token
        try:
            async with aiohttp.ClientSession() as session:
                async with session.put(_SERVICE_BASE + "/console/trucks",
                                       headers={"X-Manager-Token": _manager_token()},
                                       json=await request.json()) as response:
                    return web.Response(status=response.status, body=await response.read(),
                                        content_type="application/json")
        except aiohttp.ClientError:
            return web.json_response({"error": "请先启动管理服务，再保存启动配置"}, status=503)
    data = snapshot({t["truck_id"]: t["runtime_state"] for t in get_state_snapshot()})
    data["core_ready"] = True
    return web.json_response(data)


async def handle_console_truck_action(request):
    from aiohttp import web
    from .system_api import _authorized, _denied
    from brain.att.station import set_truck_enabled as station_set
    from config.truck_settings import get_all_truck_configs
    from paths import data_root
    # 管理服务的内部转发也必须鉴权，兼容直接打开 Core 页面时的 Web 鉴权。
    try:
        token = (data_root() / "manager" / "service_token.txt").read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not (token and request.headers.get("X-Manager-Token") == token) and not _authorized(request):
        return _denied()
    desired = read_settings()["enabled"] if request.match_info["action"] == "apply" else {key: False for key in TRUCKS}
    failed = []
    for tid, enabled in desired.items():
        try:
            if not station_set(tid, enabled):
                failed.append(tid)
            else:
                get_all_truck_configs()[tid].ENABLED = enabled
        except Exception:
            logger.exception("切换 Truck %s 失败", tid)
            failed.append(tid)
    return web.json_response({"ok": not failed, "failed": failed,
                              "error": f"未能切换: {', '.join(failed)}" if failed else None},
                             status=409 if failed else 200)
