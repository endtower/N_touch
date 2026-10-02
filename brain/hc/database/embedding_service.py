# -*- coding: utf-8 -*-
"""
向量嵌入服务（全异步，OpenAI 兼容 /embeddings 端点）

当前接入火山方舟 API（doubao-embedding-vision，2048 维），本地 Ollama 作为
备用方案（见 config/settings.py 的 VectorConfig）。
调用失败不吞错、不降级，直接上抛 NetworkError 崩溃阻断（防止检索静默失效）。
"""

import time
from typing import List, Optional

import httpx

from utils.error_handling import NetworkError
from utils.logger import get_logger

# 获取日志记录器
logger = get_logger(__name__, "INFO")

class EmbeddingService:
    """向量嵌入服务类（纯异步）"""

    def __init__(self, api_key: str, api_base: str, model: str = "doubao-embedding-vision"):
        """初始化向量嵌入服务

        Args:
            api_key: API密钥
            api_base: API基础URL
            model: 嵌入模型名称
        """
        self.api_key = api_key
        self.api_base = api_base.rstrip('/')
        self.model = model
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

    async def generate_embedding(self, text: str) -> List[float]:
        """调用嵌入模型API生成文本向量（异步，不阻塞事件循环）

        Args:
            text: 需要嵌入的文本

        Returns:
            文本的向量表示

        Raises:
            NetworkError: API调用失败（崩溃阻断语义，由上层决定终止流程）
        """
        if not text or not isinstance(text, str):
            raise ValueError("输入文本不能为空且必须是字符串")

        start_time = time.time()

        payload = {
            "input": text,
            "model": self.model
        }

        # 每次调用独立短连接（嵌入调用频度低、单条秒级，握手开销可忽略，
        # 且避免单例 ClientSession 的事件循环亲和性问题）
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{self.api_base}/embeddings",
                headers=self.headers,
                json=payload,
            )

        elapsed = time.time() - start_time
        logger.debug(f"文本嵌入API调用完成，耗时: {elapsed:.3f}秒")

        if response.status_code != 200:
            raise NetworkError(
                f"嵌入API返回 {response.status_code}: {response.text[:200]}",
                details={"model": self.model, "elapsed": round(elapsed, 3)},
            )

        result = response.json()
        embedding = result.get("data", [{}])[0].get("embedding")

        if not embedding:
            raise NetworkError(
                "API返回的嵌入结果为空",
                details={"model": self.model, "response_keys": list(result.keys())},
            )

        # token 用量统计（观测旁路）：火山 /embeddings 可能不返回 usage，缺失则跳过
        from config.llm_settings import resolve_provider_label
        from utils.llm_client_manager import record_embedding_usage
        await record_embedding_usage(
            model=self.model,
            provider=resolve_provider_label(self.api_base),
            usage=result.get("usage"),
        )

        logger.debug(f"文本嵌入生成成功，向量维度: {len(embedding)}，总耗时: {elapsed:.3f}秒")
        return embedding

# 全局嵌入服务实例
_embedding_service: Optional[EmbeddingService] = None

def get_embedding_service() -> EmbeddingService:
    """获取全局嵌入服务实例

    Returns:
        嵌入服务实例

    Raises:
        Exception: 服务未初始化时抛出异常
    """
    global _embedding_service
    if not _embedding_service:
        raise Exception("嵌入服务未初始化")
    return _embedding_service

def init_embedding_service(api_key: str, api_base: str, model: str = "doubao-embedding-vision") -> EmbeddingService:
    """初始化全局嵌入服务实例

    Args:
        api_key: API密钥
        api_base: API基础URL
        model: 嵌入模型名称

    Returns:
        嵌入服务实例
    """
    global _embedding_service
    _embedding_service = EmbeddingService(api_key, api_base, model)
    logger.debug(f"嵌入服务初始化成功，模型: {model}")
    return _embedding_service
