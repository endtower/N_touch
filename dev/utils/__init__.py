#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用工具模块

API 文档：docs/api/infrastructure.md —— 修改本包对外接口后必须同步更新该文档
（校验：python scripts/check_api_docs.py）。"""

from .logger import setup_logging, get_logger
from .error_handling import (
    AbyssCoreError,
    SystemError,
    ConfigurationError,
    ValidationError,
    MessageError,
    StorageError,
    NetworkError,
    handle_errors,
    async_handle_errors,
    get_error_info,
    setup_exception_handlers
)

__all__ = [
    "setup_logging",
    "get_logger",
    "AbyssCoreError",
    "SystemError",
    "ConfigurationError",
    "ValidationError",
    "MessageError",
    "StorageError",
    "NetworkError",
    "handle_errors",
    "async_handle_errors",
    "get_error_info",
    "setup_exception_handlers"
]