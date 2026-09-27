# -*- coding: utf-8 -*-
"""Truck 运行时控制

提供 truck 启用/禁用的运行时切换能力:
- 直接修改 truck_settings.py 中 config 实例的 ENABLED 属性 (内存生效, 不持久化)
- 通过 WS 广播变更
- 后端重启后所有覆盖自动重置为 truck_settings.py 中的默认值
"""

from typing import Dict, Any, List

from utils.logger import setup_logging

logger = setup_logging(__name__, log_level="INFO")


# truck 描述表 (给前端展示)
_TRUCK_META: Dict[str, Dict[str, Any]] = {
    "private_truck": {
        "label": "私聊卡车",
        "description": "周期扫描私聊会话, 拉取新消息",
        "category": "消息获取",
    },
    "ongoing_truck": {
        "label": "进行中任务监控",
        "description": "监控 LLM 当前正在处理的任务进度",
        "category": "监控",
    },
    "special_attention_truck": {
        "label": "特别关注用户",
        "description": "高优先级用户会话的特殊轮询",
        "category": "消息获取",
    },
    "scheduled_proactive_truck": {
        "label": "定时主动任务",
        "description": "按固定周期唤醒 proactive，做定时 check-in / 自检等主动任务",
        "category": "主动任务",
    },
    "reflection_char_truck": {
        "label": "反思触发·字符",
        "description": "未处理笔记字符数达到 500 时触发一次反思",
        "category": "反思",
    },
    "reflection_time_truck": {
        "label": "反思触发·定时",
        "description": "距上次反思满 3 小时自动触发一次反思",
        "category": "反思",
    },
    "task_truck": {
        "label": "任务到期触发",
        "description": "任务到达预计的再次处理时间时，直接启动 PFCA 处理该任务",
        "category": "任务",
    },
}


def _apply_to_config(truck_id: str, enabled: bool) -> None:
    """把单个 truck 的启用状态直接写到 truck_settings.py 的 config 实例上 (内存生效, 不持久化)"""
    try:
        from config.truck_settings import (
            private_truck_config, ongoing_truck_config,
            special_attention_truck_config, scheduled_proactive_truck_config,
            reflection_char_truck_config, reflection_time_truck_config,
            task_truck_config,
        )
        mapping = {
            "private_truck": private_truck_config,
            "ongoing_truck": ongoing_truck_config,
            "special_attention_truck": special_attention_truck_config,
            "scheduled_proactive_truck": scheduled_proactive_truck_config,
            "reflection_char_truck": reflection_char_truck_config,
            "reflection_time_truck": reflection_time_truck_config,
            "task_truck": task_truck_config,
        }
        cfg = mapping.get(truck_id)
        if cfg is not None:
            cfg.ENABLED = bool(enabled)
    except Exception as e:
        logger.warning(f"应用 truck {truck_id} 状态覆盖失败: {e}")


def init_from_disk() -> None:
    """启动时调用: 不再从磁盘加载覆盖 (配置已是临时的), 仅保留为兼容接口."""
    # 旧版本会从 web_trucks.json 加载覆盖到内存 config + station.
    # 新版本不再持久化, 后端重启时 truck_settings.py 的默认值即生效.
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

    result = []
    for tid, meta in _TRUCK_META.items():
        cfg = configs.get(tid)
        cfg_enabled = bool(getattr(cfg, "ENABLED", False)) if cfg else False
        # 真实运行状态: 如果 station 报告 running/paused, 视为启用
        real = real_states.get(tid, "unknown")
        if real in ("running", "paused"):
            actually_enabled = True
        elif real == "stopped":
            actually_enabled = False
        else:
            # station 没启动 / 未知, fallback 到 config 值
            actually_enabled = cfg_enabled
        result.append({
            "truck_id": tid,
            "label": meta["label"],
            "description": meta["description"],
            "category": meta["category"],
            "enabled": actually_enabled,
            "cycle_time": (
                getattr(cfg, "CYCLE_TIME", None)
                or getattr(cfg, "CHECK_CYCLE_TIME", None)
            ) if cfg else None,
        })
    return result


def set_truck_enabled(truck_id: str, enabled: bool) -> Dict[str, Any]:
    """设置 truck 启用状态, 返回更新后的快照

    注意: 此覆盖仅在当前进程内有效, 后端重启后会重置为 truck_settings.py 的默认值.

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