#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""全局会话状态 - 事件链周期子类（EventChainMixin）

管理事件链生命周期：
- event_chain_active：事件链是否激活
- current_event_chain：当前事件链（chain_id / start_time / end_time）
- processed_by_hc：是否已被 hc 模块处理

事件链是"一次完整处理周期"的边界语义，游标替代不了。
"""

from datetime import datetime
from typing import Dict, Any, Optional

from utils.logger import setup_logging

logger = setup_logging(__name__, log_level="INFO")


class EventChainMixin:
    """事件链周期子类"""

    # ============================================================
    # 事件链生命周期
    # ============================================================

    def start_new_event_chain(self, trigger_text: str = "", chain_id: Optional[str] = None) -> None:
        """开始新的事件链

        Args:
            trigger_text: 事件链触发原因（truck 向 PFCA 注入的第一轮消息），
                由灵光一现记事本随文档自动记录。
            chain_id: 外部预生成的链 ID（station 出队即生成，保证 proactive 阶段
                与 PFCA 阶段归属同一链）；None 则本地生成（hc 内部调用场景）。
        """
        if self._event_chain_active:
            logger.warning("事件链已处于活动状态，无需重复开启")
            return

        current_time = datetime.now().timestamp()

        self._event_chain_active = True
        self._current_event_chain = {
            "chain_id": chain_id or f"event_chain_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "start_time": current_time,
            "end_time": None,
        }
        self._processed_by_hc = False

        # 灵光一现记事本：随事件链开启创建缓存文档（缓存态，自动记录触发原因；
        # PFCA terminate 真正结束时以事件标题命名落库）
        from brain.hc.shortlight import get_shortlight_notepad
        get_shortlight_notepad().begin_chain(
            self._current_event_chain["chain_id"], trigger_text=trigger_text
        )

        self._real_time_file = self._storage_path / f"event_history_{self._current_event_chain['chain_id']}_real_time.json"

        self._notify("event_chain", active=True)
        # 事件链开始钩子：各子类（如 FrontendBroadcastMixin）在此实现自己的前处理
        self._on_event_chain_start()
        logger.info(
            f"新事件链已开始: {self._current_event_chain['chain_id']}，"
            f"开始时间: {datetime.fromtimestamp(self._current_event_chain['start_time']).strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def end_event_chain(self) -> None:
        """结束当前事件链并持久化数据"""
        if not self._event_chain_active:
            logger.warning("事件链未处于活动状态，无法结束")
            return

        current_time = datetime.now().timestamp()

        start_time = self._current_event_chain['start_time']

        self._current_event_chain["end_time"] = current_time
        self._persist_real_time()

        file_name = f"event_history_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        file_path = self._storage_path / file_name

        with open(file_path, 'w', encoding='utf-8') as f:
            import json
            json.dump(self._build_snapshot(), f, ensure_ascii=False, indent=2)

        if self._real_time_file and self._real_time_file.exists():
            self._real_time_file.unlink()

        self._event_chain_active = False
        self._current_event_chain = None
        self._real_time_file = None
        self._notify("event_chain", active=False)
        # 事件链结束钩子：各子类（如 FrontendBroadcastMixin）在此实现自己的后处理
        self._on_event_chain_end()
        # 事件链收尾：丢弃记事本缓存文档（真正结束路径已在 terminate 落库；
        # 熔断/强停等未真正结束路径不落库，缓存丢弃）
        from brain.hc.shortlight import get_shortlight_notepad
        get_shortlight_notepad().discard_chain()

        logger.info(
            f"事件链已结束并持久化: {file_path}，"
            f"持续时间: {datetime.fromtimestamp(current_time) - datetime.fromtimestamp(start_time)}"
        )

    def is_event_chain_active(self) -> bool:
        """事件链是否处于活动状态"""
        return self._event_chain_active

    def mark_as_processed_by_hc(self) -> None:
        """标记数据已被 hc 模块处理"""
        self._processed_by_hc = True
        self._persist_real_time()
        logger.debug("数据已标记为被 hc 模块处理")

    def get_event_chain(self) -> Optional[Dict[str, Any]]:
        """获取当前事件链信息"""
        return self._current_event_chain
