import requests
import time
from typing import List, Optional
from utils.logger import get_logger

# 获取日志记录器
logger = get_logger(__name__, "INFO")

class EmbeddingService:
    """向量嵌入服务类"""
    
    def __init__(self, api_key: str, api_base: str, model: str = "qwen-embedding-v1"):
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
    
    def generate_embedding(self, text: str) -> List[float]:
        """调用嵌入模型API生成文本向量
        
        Args:
            text: 需要嵌入的文本
            
        Returns:
            文本的向量表示
            
        Raises:
            Exception: API调用失败时抛出异常
        """
        if not text or not isinstance(text, str):
            raise ValueError("输入文本不能为空且必须是字符串")
        
        try:
            # 统一走 OpenAI 兼容 API（本地 Ollama / 火山方舟等端点均兼容）
            return self._call_openai_compatible_embedding(text)
        except Exception as e:
            logger.error(f"生成嵌入失败: {str(e)}")
            raise Exception(f"嵌入模型调用失败: {str(e)}")
    
    def _call_openai_compatible_embedding(self, text: str) -> List[float]:
        """使用OpenAI兼容API调用嵌入模型
        
        Args:
            text: 需要嵌入的文本
            
        Returns:
            文本的向量表示
        """
        start_time = time.time()

        payload = {
            "input": text,
            "model": self.model
        }

        # 本地端点（Ollama）绕过系统代理，避免代理软件环回转发引入额外延迟
        proxies = {"http": None, "https": None} if ("localhost" in self.api_base or "127.0.0.1" in self.api_base) else None

        response = requests.post(
            f"{self.api_base}/embeddings",
            headers=self.headers,
            json=payload,
            timeout=30,
            proxies=proxies
        )
        
        elapsed = time.time() - start_time
        logger.debug(f"文本嵌入API调用完成，耗时: {elapsed:.3f}秒")
        
        response.raise_for_status()
        
        result = response.json()
        embedding = result.get("data", [{}])[0].get("embedding")
        
        if not embedding:
            raise ValueError("API返回的嵌入结果为空")
        
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

def init_embedding_service(api_key: str, api_base: str, model: str = "qwen-embedding-v1") -> EmbeddingService:
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
