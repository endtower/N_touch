import requests
import json
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
        # 检查是否使用多模态模型
        self.is_multimodal = "vision" in model.lower() or "multimodal" in model.lower()
    
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
            # 检查是否使用多模态模型
            if self.is_multimodal:
                # 使用dashscope库的原生API调用多模态模型
                return self._call_multimodal_embedding(text)
            else:
                # 使用OpenAI兼容API调用普通模型
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
        
        response = requests.post(
            f"{self.api_base}/embeddings",
            headers=self.headers,
            json=payload,
            timeout=30
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
    
    def _call_multimodal_embedding(self, text: str) -> List[float]:
        """使用dashscope库的原生API调用多模态嵌入模型
        
        Args:
            text: 需要嵌入的文本
            
        Returns:
            文本的向量表示
        """
        start_time = time.time()
        
        try:
            import dashscope
            
            dashscope.api_key = self.api_key
            
            input_data = [{"text": text}]
            
            resp = dashscope.MultiModalEmbedding.call(
                model=self.model,
                input=input_data
            )
            
            elapsed = time.time() - start_time
            logger.debug(f"多模态嵌入API调用完成，耗时: {elapsed:.3f}秒")
            
            if hasattr(resp, 'status_code') and resp.status_code == 200:
                if hasattr(resp, 'output') and 'embeddings' in resp.output:
                    embeddings = resp.output['embeddings']
                    if embeddings and len(embeddings) > 0:
                        embedding = embeddings[0]['embedding']
                        if embedding:
                            logger.debug(f"多模态嵌入生成成功，向量维度: {len(embedding)}，总耗时: {elapsed:.3f}秒")
                            return embedding
            
            error_msg = f"API调用失败，状态码: {resp.status_code if hasattr(resp, 'status_code') else '未知'}"
            if hasattr(resp, 'message'):
                error_msg += f", 错误信息: {resp.message}"
            raise Exception(error_msg)
            
        except ImportError:
            raise Exception("请安装dashscope库: pip install dashscope")

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
