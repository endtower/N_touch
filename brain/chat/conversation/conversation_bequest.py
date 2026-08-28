# -*- coding: utf-8 -*-
from typing import List, Optional
from utils.logger import get_logger

logger = get_logger('ConversationBequest', "INFO")


class ConversationBequest:
    """Conversation 模块经验缓存管理"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not getattr(self, '_initialized', False):
            self._activated_ids: List[str] = []
            self._initialized = True
            logger.debug("ConversationBequest 初始化")

    def add(self, bequest_id: str) -> bool:
        """添加经验到缓存（去重）
        
        Returns:
            bool: True=新增成功, False=已存在
        """
        if bequest_id not in self._activated_ids:
            self._activated_ids.append(bequest_id)
            return True
        return False

    def is_activated(self, bequest_id: str) -> bool:
        """检查经验是否已激活"""
        return bequest_id in self._activated_ids

    def get_all(self) -> List[str]:
        """获取所有已激活的经验ID"""
        return self._activated_ids.copy()

    def clear(self):
        """清空缓存（会话开始时调用）"""
        self._activated_ids = []
        logger.debug("经验缓存已清空")


_bequest_instance: Optional[ConversationBequest] = None


def get_conversation_bequest() -> ConversationBequest:
    """获取 ConversationBequest 单例"""
    global _bequest_instance
    if _bequest_instance is None:
        _bequest_instance = ConversationBequest()
    return _bequest_instance