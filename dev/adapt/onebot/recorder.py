#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
adapt.onebot.recorder
=====================

原始事件录制器。

用途：
    1. 把 NapCat 推过来的每一个原始 JSON 事件完整落盘
    2. 同时落盘 parser 解析后的结果（如果有）
    3. 按天合并为单个 JSONL 文件（一行一事件），避免小文件海量堆积
    4. 落盘文件可直接作为 pytest fixture（每行一个独立 JSON 对象）

录制文件结构：

    logs/onebot_raw_events/
    ├── 2026-06-22.jsonl      ← 当天全部事件，一行一个
    ├── 2026-06-23.jsonl
    └── ...

每行一个自包含 JSON 对象：

    {"schema_version": 1, "received_at": "2026-06-22T00:45:23.123456+08:00",
     "received_at_unix": 1750569923.123, "channel": "onebot",
     "category": "message",                           // message / meta_event / notice / request / api_response / unknown
     "raw": { ... NapCat 实际推的完整 JSON ... },
     "parsed": { ... NormalizedEvent.to_system_message_dict() ... },  // 仅 message 有
     "metadata": {"self_id": 12345678, "message_id": "12345", "session_id": "group_666"}}

防爆：
    - 单日事件数 >= max_per_day 时停止录制并 WARNING
    - 超过保留期的历史日期文件（含旧格式日期目录）在启动与跨天时清理
    - 单次写入失败不影响主流程（best-effort）
"""

from __future__ import annotations

import json
import shutil
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

    def __init__(self, base_dir: str, max_per_day: int = 5000, retention_days: int = 7) -> None:
        self._base_dir = Path(base_dir)
        self._max_per_day = max_per_day
        self._retention_days = retention_days
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

            # 当天单文件：一行一事件（JSONL），避免每事件一个小文件导致
            # 目录爆炸（实测单日可产生上万文件，拖慢 IO 与目录遍历）。
            file_path = self._base_dir / f"{date_str}.jsonl"
            self._base_dir.mkdir(parents=True, exist_ok=True)

            meta = metadata or {}
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

            # 追加一行；buffering=1 保证行缓冲，进程异常退出也不丢已写行
            line = json.dumps(record_obj, ensure_ascii=False, default=str)
            with open(file_path, "a", encoding="utf-8", buffering=1) as f:
                f.write(line + "\n")

            if self._today is not None:
                self._today.count += 1

            logger.debug(f"[Recorder] appended: {file_path}")
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
            # 跨天（含进程首次记录）时清理过期录制，避免历史无限累积
            self._cleanup_old_days()
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

    def _cleanup_old_days(self) -> None:
        """删除超过保留期的录制文件。

        同时兼容历史结构：旧版本的按日目录（<date>/）也会被整体删除。
        """
        if self._retention_days <= 0 or not self._base_dir.is_dir():
            return
        cutoff = time.time() - self._retention_days * 86400
        for entry in self._base_dir.iterdir():
            try:
                if entry.is_dir():
                    # 旧格式：<date>/ 目录，整体清理
                    if entry.stat().st_mtime < cutoff:
                        shutil.rmtree(entry, ignore_errors=True)
                elif entry.suffix == ".jsonl" and entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                # 单个陈旧条目删除失败不影响其余清理（并发进程可能正占用）
                continue
