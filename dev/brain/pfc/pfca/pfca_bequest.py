# -*- coding: utf-8 -*-
from typing import List, Optional
from utils.logger import get_logger

logger = get_logger('PFCABequest', "DEBUG")


class PFCABequest:
    """PFCA 模块经验缓存管理（仅决策阶段经验）"""

    def __init__(self):
        if not getattr(self, '_initialized', False):
            self._activated_ids: List[str] = []
            self._initialized = True
            logger.debug("PFCABequest 初始化")

    def add(self, bequest_id: str) -> bool:
        """添加经验到缓存（去重，PFCA只激活一个）
        
        Returns:
            bool: True=新增成功, False=已存在
        """
        if bequest_id not in self._activated_ids:
            self._activated_ids.append(bequest_id)
            logger.debug(f"PFCA经验已激活: {bequest_id}")
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
        logger.debug("PFCA经验缓存已清空")


_bequest_instance: Optional[PFCABequest] = None


def get_pfca_bequest() -> PFCABequest:
    """获取 PFCABequest 单例"""
    global _bequest_instance
    if _bequest_instance is None:
        _bequest_instance = PFCABequest()
    return _bequest_instance