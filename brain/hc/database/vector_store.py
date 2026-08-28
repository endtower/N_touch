# -*- coding: utf-8 -*-
"""
向量存储类（SQLite）

向量以 BLOB 存 JSON 序列化，Python 侧算余弦相似度。
本模块只负责生成向量（调用嵌入服务），不直接读写数据库。
"""

from typing import List, Optional

from config.settings import vector_config
from .embedding_service import init_embedding_service, get_embedding_service
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


class VectorStore:
    """向量存储类"""

    def generate_vector(self, content: str) -> Optional[List[float]]:
        """生成文本的向量表示。"""
        if not vector_config.ENABLE_VECTOR_EMBEDDING:
            logger.debug("向量嵌入功能已禁用")
            return None

        try:
            init_embedding_service(
                api_key=vector_config.API_KEY,
                api_base=vector_config.BASE_URL,
                model=vector_config.MODEL_NAME,
            )
            embedding_service = get_embedding_service()
            vector = embedding_service.generate_embedding(content)
            logger.debug(f"成功生成文本嵌入，向量维度: {len(vector)}")
            return vector
        except Exception as e:
            logger.error(f"API调用失败: {str(e)}")
            return None


_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """获取全局向量存储实例"""
    global _vector_store
    if not _vector_store:
        _vector_store = VectorStore()
    return _vector_store
