#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
错误处理工具模块

此模块提供统一的错误处理机制，包括：
1. 统一的异常类层次结构
2. 错误处理装饰器
3. 错误日志和追踪信息收集
"""

import traceback
import functools
import logging
from typing import Callable, Any, TypeVar, cast

from .logger import get_logger

logger = get_logger(__name__)

T = TypeVar('T')

class AbyssCoreError(Exception):
    """Abyss Core系统的基础异常类"""
    error_code = 1000
    error_message = "系统错误"
    status_code = 500
    
    def __init__(self, message: str = None, details: Any = None):
        self.details = details
        self._message = message
        super().__init__(self._get_message())
    
    def _get_message(self) -> str:
        return self._message or self.error_message
    
    def to_dict(self) -> dict:
        result = {
            "error_code": self.error_code,
            "error_message": str(self),
            "status_code": self.status_code
        }
        if self.details:
            result["details"] = self.details
        return result


class SystemError(AbyssCoreError):
    error_code = 1050
    error_message = "系统核心错误"
    status_code = 500


class ConfigurationError(AbyssCoreError):
    error_code = 1100
    error_message = "配置错误"
    status_code = 500


class ValidationError(AbyssCoreError):
    error_code = 1200
    error_message = "数据验证错误"
    status_code = 400


class MessageError(AbyssCoreError):
    error_code = 1300
    error_message = "消息处理错误"
    status_code = 500


class StorageError(AbyssCoreError):
    error_code = 1400
    error_message = "存储错误"
    status_code = 500


class NetworkError(AbyssCoreError):
    error_code = 1500
    error_message = "网络错误"
    status_code = 503


class ToolCallError(AbyssCoreError):
    error_code = 1600
    error_message = "工具调用错误"
    status_code = 500


class MissingRequiredArgumentError(AbyssCoreError):
    """缺少必需参数异常"""
    error_code = 1700
    error_message = "缺少必需参数"
    status_code = 400


def handle_errors(
    error_types: tuple = (AbyssCoreError,),
    default_return: Any = None,
    log_level: int = logging.ERROR,
    raise_on: tuple = (AbyssCoreError,),
):
    """
    错误处理装饰器。

    - 异常类型在 ``error_types`` 内的，会被捕获并按 ``log_level`` 记录。
    - 落在 ``raise_on`` 子集中的异常视为"不可恢复"，记录后强制退出进程；
      其余异常按"可恢复"处理：按 ``log_level`` 记录后返回 ``default_return``，
      不阻塞主流程（适合需要把错误反馈给 LLM / 上层处理的场景）。
    - ``raise_on=None`` 表示全部按可恢复处理（绝不崩溃）。

    Args:
        error_types: 要捕获的异常类型元组（不在其中则原样抛出）。
        default_return: 可恢复异常发生时的默认返回值。
        log_level: 日志记录级别。
        raise_on: 触发"强制停止"的异常类型子集；默认仍是所有 AbyssCoreError。
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if not isinstance(e, error_types):
                    # 不在捕获范围内，原样抛出
                    raise
                _handle_caught(func.__name__, e, log_level, raise_on, default_return)
        return wrapper
    return decorator


def async_handle_errors(
    error_types: tuple = (AbyssCoreError,),
    default_return: Any = None,
    log_level: int = logging.ERROR,
    raise_on: tuple = (AbyssCoreError,),
):
    """
    异步版本的 :func:`handle_errors`，语义完全一致。
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                if not isinstance(e, error_types):
                    # 不在捕获范围内，原样抛出
                    raise
                _handle_caught(func.__name__, e, log_level, raise_on, default_return)
        return wrapper
    return decorator


def _handle_caught(func_name: str, exc: Exception, log_level: int,
                   raise_on, default_return):
    """
    统一处理被捕获的异常：区分"可恢复"与"不可恢复"。

    - 不可恢复（命中 ``raise_on``）：按 ``log_level`` 记录 + CRITICAL + os._exit(1)
    - 可恢复（未命中 ``raise_on``）：按 ``log_level`` 记录后返回 ``default_return``
    """
    is_fatal = raise_on is not None and isinstance(exc, raise_on)
    message_prefix = "异步函数" if _running_in_async_context() else "函数"
    try:
        logger.log(
            log_level,
            f"{message_prefix} {func_name} 执行出错: {exc}\n{traceback.format_exc()}",
        )
    except Exception:
        # 日志本身也不能阻塞主流程
        print(f"[error_handling] {func_name} 执行出错: {exc}")
    if is_fatal:
        for h in getattr(logger, 'handlers', []):
            try:
                h.flush()
            except Exception:
                pass
        logger.critical("检测到严重错误，强制停止程序...")
        import os
        os._exit(1)
    return default_return


def _running_in_async_context() -> bool:
    """
    简单判断当前是否在异步事件循环里被调用，用于日志前缀。
    失败/异常一律视为 sync，不抛错。
    """
    try:
        import asyncio
        asyncio.get_running_loop()
        return True
    except Exception:
        return False


def validate_required_params(*required_params):
    """
    参数验证装饰器，检查必需的参数是否都提供了。
    如果缺少参数，抛出 MissingRequiredArgumentError 异常。
    
    Args:
        *required_params: 必需的参数名称列表
        
    Returns:
        装饰后的函数
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            missing = []
            for param in required_params:
                if param not in kwargs or kwargs[param] is None:
                    missing.append(param)
            if missing:
                raise MissingRequiredArgumentError(
                    f"缺少必需参数: {', '.join(missing)}",
                    details={"missing_params": missing}
                )
            return func(*args, **kwargs)
        return wrapper
    return decorator


def async_validate_required_params(*required_params):
    """
    异步函数的参数验证装饰器，检查必需的参数是否都提供了。
    如果缺少参数，抛出 MissingRequiredArgumentError 异常。
    
    Args:
        *required_params: 必需的参数名称列表
        
    Returns:
        装饰后的异步函数
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            missing = []
            for param in required_params:
                if param not in kwargs or kwargs[param] is None:
                    missing.append(param)
            if missing:
                raise MissingRequiredArgumentError(
                    f"缺少必需参数: {', '.join(missing)}",
                    details={"missing_params": missing}
                )
            return await func(*args, **kwargs)
        return wrapper
    return decorator


def get_error_info(exc: Exception) -> dict:
    """获取异常的详细信息"""
    traceback_str = traceback.format_exc()
    
    if isinstance(exc, AbyssCoreError):
        result = exc.to_dict()
        result["error_type"] = exc.__class__.__name__
        result["stack_trace"] = traceback_str
        result["traceback"] = traceback_str
        return result
    
    return {
        "error_code": 9999,
        "error_message": str(exc),
        "status_code": 500,
        "stack_trace": traceback_str,
        "traceback": traceback_str,
        "error_type": exc.__class__.__name__
    }


def setup_exception_handlers(app):
    """设置FastAPI应用的全局异常处理器"""
    from fastapi import Request
    from fastapi.responses import JSONResponse
    
    @app.exception_handler(AbyssCoreError)
    async def abyss_core_exception_handler(request: Request, exc: AbyssCoreError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"status": "error", **exc.to_dict()}
        )
    
    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        error_info = get_error_info(exc)
        logger.error(f"发生未处理的异常: {error_info['error_message']}\n{error_info.get('stack_trace', '')}")
        return JSONResponse(
            status_code=error_info["status_code"],
            content={"status": "error", "message": "服务器内部错误"}
        )