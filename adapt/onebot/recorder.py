#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.onebot.recorder
=====================

原始事件录制器。

用途：
    1. 把 NapCat 推过来的每一个原始 JSON 事件完整落盘
    2. 同时落盘 parser 解析后的结果（如果有）
    3. 按日期/会话自动分子目录，文件命名一眼能看出内容
    4. 落盘文件可直接作为 pytest fixture（见 adapt/onebot/tests/）

录制文件结构：

    logs/onebot_raw_events/
    └── 2026-06-22/
        ├── 00-45-23_group_666_msg12345.json          ← 群消息
        ├── 00-46-11_private_10001_msg12346.json      ← 私聊消息
        ├── 00-47-02_meta_heartbeat.json              ← 心跳
        ├── 00-48-30_api_echo_ob-abc123.json          ← API 响应
        └── ...

单个录制文件内容（自包含 JSON）：

    {
        "schema_version": 1,
        "received_at": "2026-06-22T00:45:23.123456+08:00",
        "received_at_unix": 1750569923.123,
        "channel": "onebot",
        "category": "message",                        // message / meta_event / notice / request / api_response / unknown
        "raw": { ... NapCat 实际推的完整 JSON ... },
        "parsed": { ... NormalizedEvent.to_system_message_dict() ... },  // 仅 message 有
        "metadata": {
            "self_id": 12345678,
            "message_id": "12345",
            "session_id": "group_666"
        }
    }

防爆：
    - 单日文件数 >= max_per_day 时停止录制并 WARNING
    - 单次写入失败不影响主流程（best-effort）
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from utils import setup_logging

logger = setup_logging("adapt.onebot.recorder")


SCHEMA_VERSION = 1


@dataclass
class _DailyCounter:
    """单日录制计数（按日期隔离）。"""
    date: str
    count: int


class RawEventRecorder:
    """原始事件落盘器。

    用法（OneBotChannel 内部）:
        self._recorder = RawEventRecorder(
            base_dir="logs/onebot_raw_events",
            max_per_day=5000,
        )
        self._recorder.record(
            category="message",
            raw=payload,
            parsed=ev.to_system_message_dict(),
            metadata={...},
        )
    """

    def __init__(self, base_dir: str, max_per_day: int = 5000) -> None:
        self._base_dir = Path(base_dir)
        self._max_per_day = max_per_day
        self._today: Optional[_DailyCounter] = None
        self._stopped_today: bool = False

    # --------------------------------------------------------
    # 公开 API
    # --------------------------------------------------------

    def record(
        self,
        category: str,
        raw: Dict[str, Any],
        parsed: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Path]:
        """录制一个事件。

        Args:
            category: 事件分类（message / meta_event / notice / request / api_response / unknown）
            raw:      NapCat 推过来的原始 JSON
            parsed:   parser 解析后的结果（可选，message 事件才有）
            metadata: 任意附加元数据

        Returns:
            落盘文件路径，失败 / 限额时返回 None
        """
        if not self._ensure_capacity():
            return None

        try:
            now = datetime.now()
            received_at_unix = time.time()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H-%M-%S")
            ms = now.microsecond // 1000

            # 文件名：HH-MM-SS_mmm_<session>_<message_id_or_echo>.json
            meta = metadata or {}
            suffix = self._build_filename_suffix(category, meta)
            filename = f"{time_str}_{ms:03d}_{suffix}.json"

            # 当日目录
            day_dir = self._base_dir / date_str
            day_dir.mkdir(parents=True, exist_ok=True)
            file_path = day_dir / filename

            # 自包含 JSON
            record_obj = {
                "schema_version": SCHEMA_VERSION,
                "received_at": now.isoformat(),
                "received_at_unix": received_at_unix,
                "channel": "onebot",
                "category": category,
                "raw": raw,
                "parsed": parsed,
                "metadata": meta,
            }

            # 原子写：先写 .tmp 再 rename
            tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(record_obj, f, ensure_ascii=False, indent=2, default=str)
            os.replace(tmp_path, file_path)

            if self._today is not None:
                self._today.count += 1

            logger.debug(f"[Recorder] saved: {file_path}")
            return file_path
        except Exception:
            # best-effort：录制失败绝不能影响主流程
            logger.exception("[Recorder] failed to save event")
            return None

    def flush_summary(self) -> Dict[str, Any]:
        """获取当前录制状态摘要（用于日志 / 健康检查）。"""
        return {
            "base_dir": str(self._base_dir),
            "today": self._today.date if self._today else None,
            "today_count": self._today.count if self._today else 0,
            "max_per_day": self._max_per_day,
            "stopped_today": self._stopped_today,
        }

    # --------------------------------------------------------
    # 内部
    # --------------------------------------------------------

    def _ensure_capacity(self) -> bool:
        today_str = datetime.now().strftime("%Y-%m-%d")
        if self._today is None or self._today.date != today_str:
            self._today = _DailyCounter(date=today_str, count=0)
            self._stopped_today = False
        if self._stopped_today:
            return False
        if self._today.count >= self._max_per_day:
            if not self._stopped_today:
                logger.warning(
                    f"[Recorder] 达到单日限额 {self._max_per_day}，"
                    f"今天 ({today_str}) 后续事件将不再录制"
                )
                self._stopped_today = True
            return False
        return True

    @staticmethod
    def _build_filename_suffix(category: str, meta: Dict[str, Any]) -> str:
        """根据 category + metadata 拼文件后缀。"""
        if category == "message":
            sid = str(meta.get("session_id") or meta.get("scope_id") or "unknown")
            mid = str(meta.get("message_id") or meta.get("raw_message_id") or "0")
            return f"msg_{sid}_{mid}"
        if category == "api_response":
            echo = str(meta.get("echo") or "noecho")
            return f"api_{echo[:24]}"
        if category == "meta_event":
            mt = str(meta.get("meta_event_type") or "unknown")
            return f"meta_{mt}"
        if category == "notice":
            nt = str(meta.get("notice_type") or "unknown")
            return f"notice_{nt}"
        if category == "request":
            rt = str(meta.get("request_type") or "unknown")
            return f"req_{rt}"
        return f"{category}"
