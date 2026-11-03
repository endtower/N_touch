# -*- coding: utf-8 -*-
"""
向量嵌入服务（全异步）

两种接入形态（按模型名自动判定）：
- 多模态模型（doubao-embedding-vision 系列）：走火山方舟 /embeddings/multimodal，
  一次请求只处理一条文本（该接口把整条 input 合成一个向量，不能批量）。
- 文本模型（含本地 Ollama 备用）：走 OpenAI 兼容 /embeddings，按批请求。

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

    # 火山方舟 /embeddings 单次 input 上限（实测超过 10 条返回 400
    # InvalidParameter: max 10）；批量调用按该值分块，块内一次请求。
    MAX_BATCH_SIZE = 10

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
        # 多模态嵌入模型只能单条请求：/embeddings/multimodal 会把整条 input 合成一个向量
        self.multimodal = "vision" in model.lower()
        self.max_batch = 1 if self.multimodal else self.MAX_BATCH_SIZE
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        """批量生成文本向量（异步，按 MAX_BATCH_SIZE 分块，块内单次请求）。

        Args:
            texts: 需要嵌入的文本列表（非空）

        Returns:
            与输入顺序一一对应的向量列表

        Raises:
            ValueError: 输入为空或含非字符串/空串
            NetworkError: API调用失败（崩溃阻断语义，由上层决定终止流程）
        """
        if not isinstance(texts, list) or not texts:
            raise ValueError("输入文本列表不能为空")
        for text in texts:
            if not isinstance(text, str) or not text.strip():
                raise ValueError("输入文本必须是非空字符串")

        vectors: List[List[float]] = []
        for start in range(0, len(texts), self.max_batch):
            vectors.extend(await self._embed_chunk(texts[start:start + self.max_batch]))
        return vectors

    async def generate_embedding(self, text: str) -> List[float]:
        """调用嵌入模型API生成单条文本向量（内部走批量接口）。"""
        return (await self.generate_embeddings([text]))[0]

    async def _embed_chunk(self, texts: List[str]) -> List[List[float]]:
        """对一块文本发嵌入请求（多模态逐条，文本模型整块一次）。"""
        if self.multimodal:
            return [await self._embed_multimodal(text) for text in texts]
        return await self._embed_openai(texts)

    async def _embed_multimodal(self, text: str) -> List[float]:
        """火山方舟多模态嵌入接口：一条请求一条文本，返回单个向量。"""
        start_time = time.time()
        payload = {
            "model": self.model,
            "input": [{"type": "text", "text": text}],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.api_base}/embeddings/multimodal",
                headers=self.headers,
                json=payload,
            )

        elapsed = time.time() - start_time
        if response.status_code != 200:
            raise NetworkError(
                f"嵌入API返回 {response.status_code}: {response.text[:200]}",
                details={"model": self.model, "elapsed": round(elapsed, 3)},
            )

        result = response.json()
        data = result.get("data")
        embedding = data.get("embedding") if isinstance(data, dict) else None
        if not embedding:
            raise NetworkError(
                "API返回的嵌入结果为空",
                details={"model": self.model, "response_keys": list(result.keys())},
            )

        from config.llm_settings import resolve_provider_label
        from utils.llm_client_manager import record_embedding_usage
        await record_embedding_usage(
            model=self.model,
            provider=resolve_provider_label(self.api_base),
            usage=result.get("usage"),
        )
        logger.debug(
            f"多模态嵌入生成成功，向量维度: {len(embedding)}，耗时: {elapsed:.3f}秒"
        )
        return embedding

    async def _embed_openai(self, texts: List[str]) -> List[List[float]]:
        """OpenAI 兼容 /embeddings：一次请求整块文本。"""
        start_time = time.time()

        payload = {
            "input": texts,
            "model": self.model
        }

        # 每次调用独立短连接（嵌入调用频度低、单条秒级，握手开销可忽略，
        # 且避免单例 ClientSession 的事件循环亲和性问题）
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{self.api_base}/embeddings",
                headers=self.headers,
                json=payload,
            )

        elapsed = time.time() - start_time
        logger.debug(f"文本嵌入API调用完成，条数: {len(texts)}，耗时: {elapsed:.3f}秒")

        if response.status_code != 200:
            raise NetworkError(
                f"嵌入API返回 {response.status_code}: {response.text[:200]}",
                details={"model": self.model, "elapsed": round(elapsed, 3)},
            )

        result = response.json()
        data = result.get("data") or []
        if len(data) != len(texts):
            raise NetworkError(
                f"API返回的嵌入结果条数不符（请求 {len(texts)} 条，返回 {len(data)} 条）",
                details={"model": self.model, "response_keys": list(result.keys())},
            )

        # OpenAI 兼容接口按 index 标注顺序；按 index 排序后再与输入对齐
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        embeddings = [item.get("embedding") for item in ordered]
        if any(not embedding for embedding in embeddings):
            raise NetworkError(
                "API返回的嵌入结果为空",
                details={"model": self.model, "response_keys": list(result.keys())},
            )
        if len({len(embedding) for embedding in embeddings}) != 1:
            raise NetworkError(
                "同一批嵌入结果维度不一致",
                details={"model": self.model, "dims": [len(e) for e in embeddings]},
            )

        # token 用量统计（观测旁路）：火山 /embeddings 可能不返回 usage，缺失则跳过
        from config.llm_settings import resolve_provider_label
        from utils.llm_client_manager import record_embedding_usage
        await record_embedding_usage(
            model=self.model,
            provider=resolve_provider_label(self.api_base),
            usage=result.get("usage"),
        )

        logger.debug(
            f"文本嵌入生成成功，条数: {len(embeddings)}，"
            f"向量维度: {len(embeddings[0])}，总耗时: {elapsed:.3f}秒"
        )
        return embeddings

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
