#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
日志工具模块

此模块提供日志配置和管理功能，包括：
1. 设置日志级别和格式
2. 创建文件日志处理器（包括总日志和模块日志）
3. 提供日志记录器获取功能
4. 支持彩色日志输出
"""

import os
import logging
import sys
import time
import asyncio
import threading
from typing import Optional, Callable, Awaitable, List
from pathlib import Path
from dotenv import load_dotenv

# 定义ANSI颜色代码
class Colors:
    RESET = '\033[0m'
    DEBUG = '\033[36m'  # 青色
    INFO = '\033[37m'   # 白色
    WARNING = '\033[33m'  # 黄色
    ERROR = '\033[31m'  # 红色
    CRITICAL = '\033[41m\033[37m'  # 红底白字
    RED_UNDERLINE = '\033[31m\033[4m'  # 红色下划线
    TIME = '\033[32m'  # 绿色时间

# 自定义带颜色的日志格式化器
class ColoredFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt=fmt, datefmt=datefmt)
        
    def format(self, record):
        # 检查是否在终端环境
        is_tty = hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()
        
        if is_tty:
            # 根据日志级别设置不同的颜色
            level_colors = {
                logging.DEBUG: Colors.DEBUG,
                logging.INFO: Colors.INFO,
                logging.WARNING: Colors.WARNING,
                logging.ERROR: Colors.ERROR,
                logging.CRITICAL: Colors.CRITICAL,
            }
            
            # 格式化时间
            time_str = self.formatTime(record, self.datefmt)
            
            # 构建带颜色的日志消息
            level_color = level_colors.get(record.levelno, Colors.RESET)
            
            # 所有程序名都使用红色下划线样式
            name_color = Colors.RED_UNDERLINE
            
            colored_message = (
                f"{Colors.TIME}{time_str}{Colors.RESET} "
                f"[{level_color}{record.levelname}{Colors.RESET}] "
                f"{name_color}{record.name}{Colors.RESET} | "
                f"{record.getMessage()}"
            )
            
            # 如果有异常信息，添加异常信息
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = self.formatException(record.exc_info)
            if record.exc_text:
                colored_message = colored_message + "\n" + record.exc_text
            
            return colored_message
        
        # 非终端环境，使用默认格式化
        return super().format(record)

# 定义日志目录
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# 确保日志目录存在
os.makedirs(LOG_DIR, exist_ok=True)

# 设置时间格式，确保日志按时间正确排序并符合用户期望的格式
TIME_FORMAT = '%m-%d %H:%M:%S'  # 月-日 时:分:秒

# 配置需要单独记录日志的模块列表
SEPARATE_LOG_MODULES = os.getenv('SEPARATE_LOG_MODULES', '').split(',') if os.getenv('SEPARATE_LOG_MODULES') else []

# 全局总日志记录器实例
_global_logger = None

# 导出日志等级常量，方便用户直接使用而不需要导入logging模块
DEBUG = logging.DEBUG
INFO = logging.INFO
WARNING = logging.WARNING
ERROR = logging.ERROR
CRITICAL = logging.CRITICAL


def setup_global_logger() -> logging.Logger:
    """
    设置全局总日志记录器
    
    Returns:
        logging.Logger: 配置好的全局日志记录器实例
    """
    global _global_logger
    
    if _global_logger and _global_logger.handlers:
        return _global_logger
    
    # 获取项目根目录
    project_root = Path(__file__).resolve().parent.parent
    
    # 确保日志目录存在
    log_dir = project_root / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # 总日志文件名 - 不包含模块名
    global_log_file = log_dir / f"app_{time.strftime('%Y-%m-%d')}.log"
    
    # 从环境变量获取日志等级，默认为INFO
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    
    # 创建根日志记录器，这样所有模块的日志都会自动传播到它
    root_logger = logging.getLogger()
    root_logger.setLevel(level_map.get(log_level, logging.INFO))
    
    # 避免日志重复
    if not root_logger.handlers:
        # 文件处理器 - 无颜色（用于总日志文件）
        global_file_handler = logging.FileHandler(global_log_file, encoding='utf-8')
        file_formatter = logging.Formatter(
            '%(asctime)s [%(levelname)s] %(name)s | %(message)s',
            datefmt=TIME_FORMAT
        )
        global_file_handler.setFormatter(file_formatter)
        
        # 添加处理器到根日志记录器
        root_logger.addHandler(global_file_handler)
    
    # 设置_global_logger为根日志记录器
    _global_logger = root_logger
    return _global_logger


def setup_logging(name: str = 'app', log_level: Optional[int or str] = None) -> logging.Logger:
    """
    设置日志记录器
    
    Args:
        name: 日志记录器名称
        log_level: 日志级别（可以是logging.DEBUG等常量，也可以是字符串如"DEBUG"），如果不提供则从环境变量获取
    
    Returns:
        logging.Logger: 配置好的日志记录器实例
    """
    # 获取项目根目录
    project_root = Path(__file__).resolve().parent.parent
    
    # 加载.env文件
    env_path = project_root / 'config' / '.env'
    load_dotenv(env_path)
    
    # 确保日志目录存在
    log_dir = project_root / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # 日志级别映射
    level_map = {
        'DEBUG': logging.DEBUG,
        'INFO': logging.INFO,
        'WARNING': logging.WARNING,
        'ERROR': logging.ERROR,
        'CRITICAL': logging.CRITICAL
    }
    
    # 从环境变量获取日志等级，默认为INFO
    env_log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    env_level = level_map.get(env_log_level, logging.INFO)
    
    # 处理传入的日志级别
    if log_level is not None:
        # 如果是字符串，转换为logging常量
        if isinstance(log_level, str):
            final_log_level = level_map.get(log_level.upper(), env_level)
        else:
            final_log_level = log_level
    else:
        final_log_level = env_level
    
    # 创建logger实例
    logger = logging.getLogger(name)
    # 设置日志级别，但保持propagate=True，让日志能够传播到根记录器
    logger.setLevel(final_log_level)
    logger.propagate = True  # 确保日志会传播到根记录器
    
    # 避免日志重复
    if not logger.handlers:
        # 确保全局日志记录器（根记录器）已设置
        setup_global_logger()
        
        # 控制台处理器 - 有颜色
        console_handler = logging.StreamHandler()
        console_formatter = ColoredFormatter(
            '%(asctime)s [%(levelname)s] %(name)s | %(message)s',
            datefmt=TIME_FORMAT
        )
        console_handler.setFormatter(console_formatter)
        
        # 添加控制台处理器
        logger.addHandler(console_handler)
        
        # 检查是否需要为该模块创建单独的日志文件
        if name in SEPARATE_LOG_MODULES:
            module_log_file = log_dir / f"{name}_{time.strftime('%Y-%m-%d')}.log"
            module_file_handler = logging.FileHandler(module_log_file, encoding='utf-8')
            module_file_handler.setFormatter(logging.Formatter(
                '%(asctime)s [%(levelname)s] %(name)s | %(message)s',
                datefmt=TIME_FORMAT
            ))
            logger.addHandler(module_file_handler)
    
    # 日志等级已配置，此处不打印调试信息
    return logger


def get_logger(name: str, log_level: Optional[int or str] = None) -> logging.Logger:
    """
    获取或配置日志记录器
    
    Args:
        name: 日志记录器名称
        log_level: 日志级别（可以是logging.DEBUG等常量，也可以是字符串如"DEBUG"），如果不提供则使用默认等级
    
    Returns:
        logging.Logger: 配置好的日志记录器实例
    """
    logger = logging.getLogger(name)
    
    # 如果日志记录器还没有处理器，说明还没有配置
    if not logger.handlers:
        return setup_logging(name, log_level)
    
    # 如果提供了日志等级，更新日志等级
    if log_level is not None:
        set_log_level(name, log_level)
    
    return logger


def set_log_level(name: str, level: int or str):
    """
    设置日志记录器的级别
    
    Args:
        name: 日志记录器名称
        level: 日志级别（可以是logging.DEBUG等常量，也可以是字符串如"DEBUG"）
    """
    # 转换字符串级别为logging常量
    if isinstance(level, str):
        level_map = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL
        }
        level = level_map.get(level.upper(), logging.INFO)
    
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # 同时设置所有处理器的级别
    for handler in logger.handlers:
        handler.setLevel(level)


def add_separate_log_module(module_name: str):
    """
    添加需要单独记录日志的模块
    
    Args:
        module_name: 模块名称
    """
    if module_name not in SEPARATE_LOG_MODULES:
        SEPARATE_LOG_MODULES.append(module_name)
        # 重新配置该模块的日志记录器
        logger = logging.getLogger(module_name)
        for handler in logger.handlers[:]:
            if isinstance(handler, logging.FileHandler) and module_name in handler.baseFilename:
                logger.removeHandler(handler)
        setup_logging(module_name)


# ============================================================
# 前端广播： 把 WARNING 及以上级别的日志同步推送给 WebChannel,
# 前端按 type="warning" 协议弹窗展示.
# ============================================================

# notifier 签名: async def notifier(level: int, level_name: str, logger_name: str, message: str) -> None
FrontendNotifier = Callable[[int, str, str, str], Awaitable[None]]

_notifiers: List[FrontendNotifier] = []
_notifiers_lock = threading.Lock()
_handler_attached: bool = False


class FrontendNotifyHandler(logging.Handler):
    """拦截 WARNING/ERROR/CRITICAL 级别日志, 调度所有已注册的 notifier.

    notifier 是 async 函数, 但 logging.Handler.emit() 是同步调用;
    这里通过 asyncio.run_coroutine_threadsafe() 把协程丢进事件循环,
    如果当前线程已经在目标 loop 里, 也安全. 不抛异常给上游,
    避免日志系统本身因为通知失败而中断.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = record.levelno
            level_name = record.levelname
            logger_name = record.name
            message = record.getMessage()

            with _notifiers_lock:
                notifiers = list(_notifiers)
            if not notifiers:
                return

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # 没在 asyncio loop 内 (例如 LLM 同步线程), 直接跳过
                return

            for ntf in notifiers:
                try:
                    asyncio.run_coroutine_threadsafe(
                        ntf(level, level_name, logger_name, message),
                        loop,
                    )
                except Exception:
                    # 单个 notifier 失败不影响其他, 也不影响日志本身
                    pass
        except Exception:
            # 兜底: 任何意外都不能影响正常的日志输出
            pass


def register_frontend_notifier(notifier: FrontendNotifier) -> None:
    """注册一个前端通知器 (异步回调). 同一函数多次注册会重复触发, 调用方自行去重."""
    global _handler_attached
    with _notifiers_lock:
        if notifier not in _notifiers:
            _notifiers.append(notifier)
        need_attach = not _handler_attached
        if need_attach:
            _handler_attached = True

    if need_attach:
        # 确保全局 logger 已就位, 然后把 FrontendNotifyHandler 挂到根 logger 上,
        # 这样任何模块的子 logger (logger.propagate=True) 都会冒泡到这里
        root = setup_global_logger()
        if not any(isinstance(h, FrontendNotifyHandler) for h in root.handlers):
            root.addHandler(FrontendNotifyHandler())


def unregister_frontend_notifier(notifier: FrontendNotifier) -> None:
    """反注册一个前端通知器. 若没有 notifier 了, 自动摘除 handler."""
    global _handler_attached
    with _notifiers_lock:
        if notifier in _notifiers:
            _notifiers.remove(notifier)
        has_any = bool(_notifiers)
        if not has_any:
            _handler_attached = False

    if not has_any:
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, FrontendNotifyHandler):
                root.removeHandler(h)