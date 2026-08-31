# -*- coding: utf-8 -*-
"""
LLM客户端管理器

提供统一的LLM调用接口，支持4种模式：
- advanced-reason: Pro模型 + 思考模式
- advanced: Pro模型 + 非思考模式
- basic-reason: 普通模型 + 思考模式
- basic: 普通模型 + 非思考模式
"""

import inspect
import json
from pathlib import Path
from pickle import TRUE
import openai
from typing import Dict, Any, List, Optional
from utils.logger import get_logger
from config.settings import LLMConfig

logger = get_logger('LLMClientManager', log_level='INFO')

TOKEN_STATS_FILE = Path(__file__).parent.parent / "token_stats.json"
TOKEN_TRACE_FILE = Path(__file__).parent.parent / "token_trace.json"

TOKEN_PRICING = {
    "deepseek-v4-flash": {
        "input_per_million": 1,
        "output_per_million": 2,
        "cached_per_million": 0.02
    },
    "deepseek-v4-pro": {
        "input_per_million": 3,
        "output_per_million": 6,
        "cached_per_million": 0.025
    }
}


class LLMClient:
    """LLM客户端类"""
    
    def __init__(self):
        self._client = openai.AsyncOpenAI(
            api_key=LLMConfig.API_KEY,
            base_url=LLMConfig.BASE_URL
        )
    
    async def chat(self, model: str, messages: List[Dict], extra_body: Dict = None, 
                  caller_file: str = None, caller_name: str = None, caller_line: int = None, **kwargs):
        """内部聊天方法"""
        request_params = {
            "model": model,
            "messages": messages,
        }
        
        if extra_body:
            request_params["extra_body"] = extra_body
        
        request_params.update(kwargs)
        
        response = await self._client.chat.completions.create(**request_params)
        
        self._log_usage(response, model, caller_file, caller_name, caller_line)
        
        return response
    
    def _log_usage(self, response, model: str, caller_file: str, caller_name: str, caller_line: int):
        """记录token消耗"""
        usage = response.usage
        
        cache_hit = getattr(usage, 'prompt_cache_hit_tokens', 0) or 0
        cache_miss = getattr(usage, 'prompt_cache_miss_tokens', 0) or 0
        cached = (getattr(usage.prompt_tokens_details, 'cached_tokens', 0) or 0) if usage.prompt_tokens_details else 0
        reasoning = (usage.completion_tokens_details.reasoning_tokens or 0) if usage.completion_tokens_details else 0
        
        file_display = caller_file.split('\\')[-1] if caller_file else 'unknown'
        
        self._save_stats(file_display, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens, 
                        cached, cache_hit, cache_miss, reasoning, model)
    
    def _format_cost(self, value: float) -> str:
        """格式化金额，避免科学计数法"""
        if value == 0:
            return "0.00000000"
        if abs(value) < 0.00000001:
            return f"{value:.10f}".rstrip('0')
        return f"{value:.8f}".rstrip('0').rstrip('.')
    
    def _save_stats(self, file_name: str, prompt: int, completion: int, total: int, cached: int, 
                cache_hit: int, cache_miss: int, reasoning: int, model: str):
        """保存token统计到文件"""
        try:
            stats = {}
            if TOKEN_STATS_FILE.exists():
                with open(TOKEN_STATS_FILE, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
            
            if file_name not in stats:
                stats[file_name] = {
                    "call_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cached_tokens": 0,
                    "cache_hit_tokens": 0,
                    "cache_miss_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost_cache_miss": "0",
                    "cost_cache_hit": "0",
                    "cost_output": "0",
                    "cost_output_content": "0",
                    "cost_output_reasoning": "0",
                    "cost_total": "0",
                    "models": []
                }
            
            pricing = TOKEN_PRICING.get(model, TOKEN_PRICING["deepseek-v4-flash"])
            
            cost_cache_miss = cache_miss / 1_000_000 * pricing["input_per_million"]
            cost_cache_hit = cache_hit / 1_000_000 * pricing["cached_per_million"]
            
            content_tokens = completion - reasoning
            cost_output_content = content_tokens / 1_000_000 * pricing["output_per_million"]
            cost_output_reasoning = reasoning / 1_000_000 * pricing["output_per_million"]
            cost_output = cost_output_content + cost_output_reasoning
            
            cost_total = cost_cache_miss + cost_cache_hit + cost_output
            
            stats[file_name]["call_count"] += 1
            stats[file_name]["prompt_tokens"] += prompt
            stats[file_name]["completion_tokens"] += completion
            stats[file_name]["total_tokens"] += total
            stats[file_name]["cached_tokens"] += cached
            stats[file_name]["cache_hit_tokens"] += cache_hit
            stats[file_name]["cache_miss_tokens"] += cache_miss
            stats[file_name]["reasoning_tokens"] += reasoning
            stats[file_name]["cost_cache_miss"] = self._format_cost(float(stats[file_name].get("cost_cache_miss", "0")) + cost_cache_miss)
            stats[file_name]["cost_cache_hit"] = self._format_cost(float(stats[file_name].get("cost_cache_hit", "0")) + cost_cache_hit)
            stats[file_name]["cost_output"] = self._format_cost(float(stats[file_name].get("cost_output", "0")) + cost_output)
            stats[file_name]["cost_output_content"] = self._format_cost(float(stats[file_name].get("cost_output_content", "0")) + cost_output_content)
            stats[file_name]["cost_output_reasoning"] = self._format_cost(float(stats[file_name].get("cost_output_reasoning", "0")) + cost_output_reasoning)
            stats[file_name]["cost_total"] = self._format_cost(float(stats[file_name].get("cost_total", "0")) + cost_total)
            
            if model not in stats[file_name]["models"]:
                stats[file_name]["models"].append(model)
            
            with open(TOKEN_STATS_FILE, 'w', encoding='utf-8') as f:
                json.dump(stats, f, indent=2, ensure_ascii=False)
            
            if LLMConfig.ENABLE_TOKEN_TRACE:
                self._save_trace(file_name, prompt, completion, total, cached, cache_hit, cache_miss, reasoning, model, 
                               cost_cache_miss, cost_cache_hit, cost_output_content, cost_output_reasoning)
                
        except Exception as e:
            logger.warning(f"保存token统计失败: {e}")
    
    def _save_trace(self, file_name: str, prompt: int, completion: int, total: int, cached: int,
                    cache_hit: int, cache_miss: int, reasoning: int, model: str,
                    cost_cache_miss: float, cost_cache_hit: float, cost_output_content: float, cost_output_reasoning: float):
        """保存token消耗顺序记录"""
        try:
            trace = []
            if TOKEN_TRACE_FILE.exists():
                with open(TOKEN_TRACE_FILE, 'r', encoding='utf-8') as f:
                    trace = json.load(f)
            
            from datetime import datetime
            trace.append({
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "file": file_name,
                "model": model,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": total,
                "cached_tokens": cached,
                "cache_hit_tokens": cache_hit,
                "cache_miss_tokens": cache_miss,
                "reasoning_tokens": reasoning,
                "cost_cache_miss": self._format_cost(cost_cache_miss),
                "cost_cache_hit": self._format_cost(cost_cache_hit),
                "cost_output_content": self._format_cost(cost_output_content),
                "cost_output_reasoning": self._format_cost(cost_output_reasoning),
                "cost_total": self._format_cost(cost_cache_miss + cost_cache_hit + cost_output_content + cost_output_reasoning)
            })
            
            with open(TOKEN_TRACE_FILE, 'w', encoding='utf-8') as f:
                json.dump(trace, f, indent=2, ensure_ascii=False)
                
        except Exception as e:
            logger.warning(f"保存token跟踪失败: {e}")


class LLMBridge:
    """LLM调用桥接器（单例由模块级 llm_bridge 持有）"""

    MODES = {
        'advanced-reason': {
            'model': 'deepseek-v4-pro',
            'thinking': True
        },
        'advanced': {
            'model': 'deepseek-v4-pro',
            'thinking': False
        },
        'basic-reason': {
            'model': 'deepseek-v4-flash',
            'thinking': True
        },
        'basic': {
            'model': 'deepseek-v4-flash',
            'thinking': False
        }
    }

    def __init__(self):
        self._client = LLMClient()

    async def call_llm(
        self,
        mode: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        caller_file: str = None,
        caller_name: str = None,
        caller_line: int = None,
        **kwargs
    ) -> Any:
        """
        调用LLM的统一接口
        
        Args:
            mode: 调用模式，可选值：
                - advanced-reason: Pro模型 + 思考模式
                - advanced: Pro模型 + 非思考模式
                - basic-reason: 普通模型 + 思考模式
                - basic: 普通模型 + 非思考模式
            messages: 消息列表 (必需)
            tools: 工具列表 (可选)
            **kwargs: 其他传递给API的参数
        
        Returns:
            LLM响应对象
        """
        if mode not in self.MODES:
            raise ValueError(f"未知的调用模式: {mode}，支持的模式: {list(self.MODES.keys())}")
        
        mode_config = self.MODES[mode]
        extra_body = None
        
        if mode_config['thinking']:
            extra_body = {"thinking": {"type": "enabled"}}
        else:
            extra_body = {"thinking": {"type": "disabled"}}
        
        request_params = {
            "model": mode_config['model'],
            "messages": messages,
        }
        
        if tools is not None:
            request_params["tools"] = tools
        
        request_params.update(kwargs)
        
        logger.debug(f"LLM调用 - 模式: {mode}, 模型: {mode_config['model']}, 思考: {mode_config['thinking']}")
        
        return await self._client.chat(**request_params, extra_body=extra_body,
                                       caller_file=caller_file, caller_name=caller_name, caller_line=caller_line)


llm_bridge = LLMBridge()


async def call_llm(
    mode: str,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    **kwargs
) -> Any:
    """
    调用LLM的统一接口
    
    Args:
        mode: 调用模式，可选值：
            - advanced-reason: Pro模型 + 思考模式
            - advanced: Pro模型 + 非思考模式
            - basic-reason: 普通模型 + 思考模式
            - basic: 普通模型 + 非思考模式
        messages: 消息列表 (必需)
        tools: 工具列表 (可选)
        **kwargs: 其他传递给API的参数
    
    Returns:
        LLM响应对象
    """
    caller_frame = inspect.currentframe().f_back
    caller_info = caller_frame.f_code
    caller_file = caller_info.co_filename
    caller_name = caller_info.co_name
    caller_line = caller_frame.f_lineno
    
    return await llm_bridge.call_llm(mode, messages, tools, caller_file=caller_file, caller_name=caller_name, caller_line=caller_line, **kwargs)
