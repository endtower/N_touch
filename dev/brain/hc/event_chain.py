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

        from config.settings import chat_config
        from brain.hc.event_chain_permissions import EventChainPermissions
        permissions = EventChainPermissions(
            chain_id or f"event_chain_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            chat_config.SOCIAL_CHAT_SEND_MODE.strip().lower(),
            default_notify=chat_config.SOCIAL_CHAT_UNRESTRICTED_NOTIFY,
            timed_grants=self._timed_social_grants,
        )
        from brain.hc.shortlight import get_shortlight_notepad
        notepad = get_shortlight_notepad()

        self._event_chain_active = True
        self._current_event_chain = {
            "chain_id": permissions.chain_id,
            "start_time": current_time,
            "end_time": None,
        }
        self._processed_by_hc = False
        self._event_chain_permissions = permissions

        # 灵光一现记事本：随事件链开启创建缓存文档（缓存态，自动记录触发原因；
        # PFCA terminate 真正结束时以事件标题命名落库）
        notepad_started = False
        try:
            notepad.begin_chain(
                self._current_event_chain["chain_id"], trigger_text=trigger_text
            )
            notepad_started = True
            self._real_time_file = self._storage_path / f"event_history_{permissions.chain_id}_real_time.json"
            self._notify("event_chain", active=True)
            self._on_event_chain_start()
        except BaseException:
            permissions.close()
            self._event_chain_permissions = None
            self._event_chain_active = False
            self._current_event_chain = None
            self._real_time_file = None
            if notepad_started:
                notepad.discard_chain()
            raise
        logger.info(
            f"新事件链已开始: {self._current_event_chain['chain_id']}，"
            f"开始时间: {datetime.fromtimestamp(self._current_event_chain['start_time']).strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def end_event_chain(self) -> None:
        """结束当前事件链并持久化数据"""
        if not self._event_chain_active:
            logger.warning("事件链未处于活动状态，无法结束")
            return

        # 先撤销权限；即使后续归档失败，旧 Agent 也不能继续自由发送。
        self.get_event_chain_permissions().close()
        try:
            self._archive_event_chain()
        finally:
            self._event_chain_permissions = None
            self._event_chain_active = False
            self._current_event_chain = None
            self._real_time_file = None
            from brain.hc.shortlight import get_shortlight_notepad
            get_shortlight_notepad().discard_chain()
            self._notify("event_chain", active=False)
            self._on_event_chain_end()

    def get_event_chain_permissions(self):
        """取得当前链唯一的权限服务；链外调用直接阻断。"""
        self._require_event_chain_active()
        permissions = self._event_chain_permissions
        if permissions is None:
            raise RuntimeError("活动事件链缺少权限服务")
        permissions.require_active()
        return permissions

    def _archive_event_chain(self) -> None:
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
