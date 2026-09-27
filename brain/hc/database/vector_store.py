# -*- coding: utf-8 -*-
"""
向量存储类（SQLite）

向量以 BLOB 存 JSON 序列化，Python 侧算余弦相似度。
本模块只负责生成向量（调用嵌入服务），不直接读写数据库。

失败语义（fail-fast）：嵌入 API 调用失败时直接上抛异常崩溃阻断，
不吞错返回 None 降级——防止检索静默失效、错误扩大蔓延。
仅 ENABLE_VECTOR_EMBEDDING=False（配置显式停用）时返回 None。
"""

from typing import List, Optional

from config.settings import vector_config
from .embedding_service import init_embedding_service, get_embedding_service
from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


class VectorStore:
    """向量存储类"""

    async def generate_vector(self, content: str, target: str = "bequest") -> Optional[List[float]]:
        """生成文本的向量表示（异步）。

        Args:
            content: 待向量化的文本
            target: 向量化目标（"bequest" / "character" / "group" / "task"）。
                VectorConfig.ONLY_BEQUEST_VECTORIZATION=True 时非 bequest 目标
                直接返回 None（不调 API），生成侧写 NULL、检索侧返回空。

        Raises:
            NetworkError: 嵌入 API 调用失败（崩溃阻断，不降级）。
        """
        if not vector_config.ENABLE_VECTOR_EMBEDDING:
            logger.debug("向量嵌入功能已禁用")
            return None

        if vector_config.ONLY_BEQUEST_VECTORIZATION and target != "bequest":
            logger.debug(f"仅经验向量化模式开启，跳过 {target} 向量化")
            return None

        init_embedding_service(
            api_key=vector_config.API_KEY,
            api_base=vector_config.BASE_URL,
            model=vector_config.MODEL_NAME,
        )
        embedding_service = get_embedding_service()
        vector = await embedding_service.generate_embedding(content)
        logger.debug(f"成功生成文本嵌入，向量维度: {len(vector)}")
        return vector


_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """获取全局向量存储实例"""
    global _vector_store
    if not _vector_store:
        _vector_store = VectorStore()
    return _vector_store
