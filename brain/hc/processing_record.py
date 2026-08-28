#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""全局会话状态 - 处理记录子类（ProcessingRecordMixin）

管理处理记录：
- pfca_actions：PFCA 中循环行为记录
- emotion_results：会话情绪结果
"""

from datetime import datetime
from typing import Dict, Any, List

from utils.logger import setup_logging

logger = setup_logging(__name__, log_level="INFO")


class ProcessingRecordMixin:
    """处理记录子类"""

    # ============================================================
    # PFCA 行为记录（中循环）
    # ============================================================

    def add_pfca_action(self, action: str) -> None:
        """添加一条 PFCA 行为记录（中循环）"""
        if not self._event_chain_active:
            logger.warning("事件链未处于活动状态，无法管理 pfca_actions")
            return

        action_record = {
            "action": action,
            "timestamp": datetime.now().timestamp(),
        }
        self._data["processing"]["pfca_actions"].append(action_record)
        self._persist_real_time()
        logger.debug(f"事件链 {self._current_event_chain['chain_id']} 行为已添加: {action}")

    def clear_pfca_actions(self) -> None:
        """清空 PFCA 行为记录（中循环）"""
        if not self._event_chain_active:
            logger.warning("事件链未处于活动状态，无法清空 pfca_actions")
            return

        self._data["processing"]["pfca_actions"] = []
        self._persist_real_time()
        logger.info("pfca_actions已清空")

    def manager_pfca_actions(self, operation: str, action: str = None) -> None:
        """管理 pfca_actions"""
        if operation == 'add' and action:
            self.add_pfca_action(action)
        elif operation == 'clear':
            self.clear_pfca_actions()
        else:
            logger.warning(f"不支持的操作类型: {operation}")

    # ============================================================
    # 情绪结果记录
    # ============================================================

    def add_emotion_result(
        self,
        conversation_key: str,
        session_id: str,
        session_name: str,
        start_time: float,
        end_time: float,
        emotion_type: str,
        pfca_strategy: str = "",
        exit_reason: str = "",
        encounters: List[str] = None,
        activated_bequests: List[str] = None
    ) -> None:
        """记录会话情绪结果（同一会话周期只记录一次）

        Args:
            conversation_key: 会话周期标识（用于判断是否已记录）
            session_id: 会话标识，格式为 "type_id"，如 "private_1234567890" / "group_198431299"
            session_name: 会话名称
            start_time: 对话开始时间戳
            end_time: 对话结束时间戳
            emotion_type: 情绪类型（dopamine）
            pfca_strategy: PFCA策略指令
            exit_reason: 退出说明
            encounters: 遭遇情况列表
            activated_bequests: 已激活的经验列表
        """
        if not self._event_chain_active:
            logger.warning("事件链未处于活动状态，无法记录情绪结果")
            return

        if encounters is None:
            encounters = []
        if activated_bequests is None:
            activated_bequests = []

        record = {
            "conversation_key": conversation_key,
            "session_id": session_id,
            "session_name": session_name,
            "start_time": start_time,
            "end_time": end_time,
            "emotion_type": emotion_type,
            "pfca_strategy": pfca_strategy,
            "exit_reason": exit_reason,
            "encounters": encounters,
            "activated_bequests": activated_bequests,
            "timestamp": datetime.now().timestamp(),
        }
        self._data["processing"]["emotion_results"].append(record)
        self._persist_real_time()
        logger.debug(f"情绪结果已记录: {session_name} {emotion_type}")

    def get_emotion_results(self) -> List[Dict]:
        """获取所有情绪结果"""
        return self._data.get("processing", {}).get("emotion_results", [])
