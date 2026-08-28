#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
system_core.py

系统核心协调器，负责管理整个系统的生命周期和各个模块的协调工作。

全链路统一 async/await：
- start / stop / add_message 均为 async（依赖 MessageBus 已切到 asyncio）
- 同步构造只做轻量初始化（DB 连接、handler 实例化），不启动任何后台任务
"""


from pathlib import Path

# 导入日志模块
from utils import setup_logging,async_handle_errors, SystemError

# 导入消息处理器模块
from memory.message_handler import get_message_handler

# 导入消息总线模块
from core.message_bus import get_message_bus, reset_message_bus

# 导入注意力核心模块
from brain.att import get_attention_core

# 导入AMY杏仁核模块
from brain.amy import get_emotional_interface

# 导入 adapt 平台适配层
from adapt import get_channel_manager, reset_channel_manager
from adapt.onebot import OneBotChannel, OneBotChannelConfig
from adapt.web import WebChannel, WebChannelConfig
# adapt 配置文件（OneBot / WeChat / Web）
from config.adapt_settings import (
    is_onebot_enabled,
    get_onebot_config,
    is_web_enabled,
    get_web_config,
)


# 创建日志记录器
logger = setup_logging('system_core')


class SystemCore:
    """系统核心协调器类，支持异步上下文管理器"""
    # 单例实例
    _instance = None

    def __new__(cls):
        """确保只创建一个SystemCore实例"""
        if cls._instance is None:
            cls._instance = super(SystemCore, cls).__new__(cls)
            # 同步初始化只做轻量无 IO 的事情
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """同步初始化（无 IO，可安全在 __new__ 中调用）"""
        self.is_running = False
        self.message_bus = None  # 由 async start() 在 loop 内 await get_message_bus() 注入

        # 初始化记忆数据库连接（SQLite，懒连接，无 IO）
        logger.info("开始初始化记忆数据库连接...")
        from brain.hc.database.core import get_memory_db
        self.memory_db = get_memory_db()
        logger.info("记忆数据库连接初始化完成")

        # 初始化消息处理器（同步，handler 本身无 IO）
        self.message_processor = get_message_handler()
        # 初始化注意力核心
        self.attention_core = get_attention_core()
        # 初始化AMY杏仁核模块
        self.amy_core = None

    @async_handle_errors(error_types=(SystemError,), default_return=None)
    async def start(self):
        """异步启动SystemCore及相关模块"""
        if self.is_running:
            logger.warning("SystemCore已经处于运行状态")
            return

        # 在已运行的 asyncio loop 内拿到 MessageBus 单例并启动
        self.message_bus = await get_message_bus()
        await self.message_bus.start()

        # 订阅 user_message 主题（subscribe 是异步元数据操作）
        await self.message_bus.subscribe("user_message", self._handle_user_message)

        # 初始化默认值预设：必须在 async 上下文里 await，否则会丢 RuntimeWarning。
        # 这里直接抛错阻断：默认角色是后续人格 / impress / vector 等能力的前提，
        # 按 user_rules 关键方法失败应崩溃阻断，禁止静默吞。
        from brain.hc.database.default_initializer import DefaultInitializer
        await DefaultInitializer.initialize_all()
        logger.info("默认值预设初始化完成")

        # 启动 adapt channels（NapCat 等平台的反向 WS 服务端）
        mgr = await get_channel_manager()
        if is_onebot_enabled():
            await mgr.add(OneBotChannel(
                OneBotChannelConfig.from_platform_config(get_onebot_config())
            ))
        else:
            logger.info("OneBot 平台未启用，跳过注册")

        # Web 通道（浏览器前端），跟 OneBot 可以并存；NapCat 没起时也能独立用
        if is_web_enabled():
            await mgr.add(WebChannel(
                WebChannelConfig.from_platform_config(get_web_config())
            ))
        else:
            logger.info("Web 平台未启用，跳过注册")

        # 把已注册的所有 channel 一次性 start
        await mgr.start_all()

        # 启动消息处理器
        self.message_processor.start()
        # 启动注意力核心（与主 loop 共享）
        await self.attention_core.start()
        # 启动AMY杏仁核线程
        if self.amy_core is None:
            self.amy_core = await get_emotional_interface()

        self.is_running = True
        logger.info("SystemCore初始化完成")

    @async_handle_errors(error_types=(SystemError,), default_return=None)
    async def stop(self):
        """异步停止SystemCore及相关模块"""
        if not self.is_running:
            logger.warning("SystemCore已经处于停止状态")
            return

        # 先停 adapt（最外层，先切断外部事件流入）
        mgr = await get_channel_manager()
        await mgr.stop_all()

        # 停止消息处理器
        self.message_processor.stop()
        # 停止注意力核心（async，必须 await；否则 station 的无限循环 task 不退出，
        # uvicorn shutdown 时 event loop 会一直等它，导致 Ctrl+C 无法退出进程）
        await self.attention_core.stop()
        # 停止消息总线
        if self.message_bus is not None:
            await self.message_bus.stop()

        # 关闭记忆数据库连接
        if hasattr(self, 'memory_db'):
            logger.info("开始关闭记忆数据库连接...")
            await self.memory_db.close()
            logger.info("记忆数据库连接已关闭")

        # 关闭消息仓库连接（aiosqlite 的 worker 线程是非 daemon 的，
        # 不关闭会导致进程退出时被卡住，Ctrl+C 无法释放终端）
        from memory.message_repository import get_message_store
        await get_message_store().close()

        # 重置单例，确保下次启动是干净状态
        SystemCore._instance = None
        await reset_message_bus()      # 重置 MessageBus 单例
        await reset_channel_manager()  # 重置 ChannelManager 单例

        self.is_running = False
        logger.info("SystemCore已停止")

    @async_handle_errors(error_types=(SystemError,), default_return=None)
    async def __aenter__(self):
        """异步上下文管理器进入方法"""
        await self.start()
        return self

    @async_handle_errors(error_types=(SystemError,), default_return=None)
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出方法"""
        await self.stop()
        if exc_type is not None:
            logger.error(f"SystemCore上下文退出时发生异常: {exc_val}")
        return False  # 不抑制异常

    @async_handle_errors(error_types=(SystemError,), default_return=False)
    async def add_message(self, message: dict):
        """异步添加消息到系统处理

        Args:
            message: 待处理的消息数据，包含session_info和message_list字段
        """
        if not self.is_running or self.message_bus is None:
            logger.warning("SystemCore未运行，消息未处理")
            return False

        await self.message_bus.publish("user_message", message)

        # 记录消息接收信息
        if 'message_list' in message and message['message_list']:
            user_ids = [msg.get('user_id', 'unknown') for msg in message['message_list']]
            message_ids = [msg.get('message_id', 'unknown') for msg in message['message_list']]
            logger.debug(f"SystemCore已接收消息: 用户ID={user_ids}, 消息ID={message_ids}")
        else:
            logger.info("SystemCore已接收消息")

        return True

    async def _handle_user_message(self, message):
        """处理用户消息的回调（由 MessageBus 在其 loop 内 await 调用）"""
        logger.debug("接收到用户消息并传递给消息处理器")
        await self.message_processor.process_message(message)