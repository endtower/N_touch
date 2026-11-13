"""独立于 core 的 Truck 启动配置；管理页和 core 共用同一份原子存储。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from paths import data_root


# (显示名、说明、分类、默认启用、检查周期)。这里是启动默认值的唯一来源。
TRUCKS = {
    "private_truck": ("私聊卡车", "周期扫描私聊会话，拉取新消息", "消息获取", True, 1.0),
    "at_mention_truck": ("群聊被@检测", "群里有消息@到助手本人时唤醒", "消息获取", True, 5.0),
    "pre_truck": ("PRE 契机", "PRE契机：定时巡检任务看能不能提前执行；群聊里主人有新消息时也会偶尔看看，交给判断层决定是否主动", "主动任务", True, 60.0),
    "reflection_char_truck": ("反思触发·字符", "未处理笔记达到字符阈值时反思", "反思", True, 60.0),
    "reflection_time_truck": ("反思触发·定时", "按间隔检查反思条件", "反思", False, 60.0),
    "reflection_efficacy_truck": ("效能感反思", "空闲时降为0触发自修正；达到100且维护条件满足时触发专题维护", "反思", False, 1.0),
    "reflection_belonging_truck": ("归属感反思", "空闲时降为0触发补偿性反思；达到100且没有未完成的关系建设任务时触发关系建设", "反思", False, 1.0),
    "task_truck": ("任务到期触发", "任务到期时启动处理", "任务", True, 60.0),
}
_lock = threading.Lock()


def settings_path() -> Path:
    return data_root() / "manager" / "trucks.json"


def read_settings() -> dict:
    defaults = {key: meta[3] for key, meta in TRUCKS.items()}
    path = settings_path()
    if not path.exists():
        return {"version": 1, "enabled": defaults, "startup_paused": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("enabled", {}), dict):
        raise ValueError("Truck 配置格式无效")
    if type(data.get("startup_paused", False)) is not bool:
        raise ValueError("startup_paused 必须是布尔值")
    for key, value in data.get("enabled", {}).items():
        # 旧版本管理壳（如冻结版 manager.exe）不认识已移除的 truck，
        # 保存时会把 legacy key 写回。读取端忽略即可，不能因此启动失败；
        # 写入端的输入校验仍然严格（update_settings 拒绝未知 key）。
        if key not in TRUCKS:
            continue
        if type(value) is not bool:
            raise ValueError(f"无效的 Truck 配置: {key}")
        defaults[key] = value
    return {"version": 1, "enabled": defaults, "startup_paused": data.get("startup_paused", False)}


def update_settings(updates: dict) -> dict:
    if not isinstance(updates, dict) or set(updates) - {"enabled", "startup_paused"}:
        raise ValueError("只允许修改 enabled 和 startup_paused")
    enabled = updates.get("enabled", {})
    if not isinstance(enabled, dict):
        raise ValueError("enabled 必须是对象")
    for key, value in enabled.items():
        if key not in TRUCKS or type(value) is not bool:
            raise ValueError(f"无效的 Truck 配置: {key}")
    if "startup_paused" in updates and type(updates["startup_paused"]) is not bool:
        raise ValueError("startup_paused 必须是布尔值")
    with _lock:
        data = read_settings()
        data["enabled"].update(enabled)
        if "startup_paused" in updates:
            data["startup_paused"] = updates["startup_paused"]
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="trucks-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return data


def snapshot(runtime: dict[str, str] | None = None) -> dict:
    data = read_settings()
    data["trucks"] = [
        {"truck_id": key, "label": meta[0], "description": meta[1], "category": meta[2],
         "configured_enabled": data["enabled"][key], "enabled": (runtime or {}).get(key) in ("running", "paused"),
         "runtime_state": (runtime or {}).get(key, "stopped"), "cycle_time": meta[4]}
        for key, meta in TRUCKS.items()
    ]
    return data


def apply_startup_settings(configs: dict) -> None:
    data = read_settings()
    for key, cfg in configs.items():
        cfg.ENABLED = data["enabled"][key] and not data["startup_paused"]
