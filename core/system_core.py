#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
system_core.py

系统核心协调器，负责管理整个系统的生命周期和各个模块的协调工作。

全链路统一 async/await。SystemCore 是普通类（由 main.py 的 lifespan 单持有，
不再是 __new__ 单例），通过 ServiceRegistry 按序启停各服务：

    注册顺序 = 启动顺序：  memory_db → message_store → default_initializer
                           → amy → attention → message_handler → channels
    stop_all 逆序执行：    channels（先切断外部事件流入）→ message_handler
                           → attention → amy → default_initializer
                           → message_store → memory_db（存储最后关闭）

- start 失败：ServiceRegistry 回滚已启动项后原样抛出 → lifespan 收到异常，
  uvicorn 非零退出（不做 os._exit，保留完整 traceback）。
- stop 阶段：单项失败由 registry 记日志继续，不互相阻断。
"""

from utils import setup_logging
from core.lifecycle import FuncService, ServiceRegistry

# 导入消息处理器模块
from memory.message_handler import get_message_handler

# 导入注意力核心模块
from brain.att import get_attention_core

# 导入AMY杏仁核模块
from brain.amy import get_emotional_interface, reset_emotional_interface

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
    """系统核心协调器类，支持异步上下文管理器。

    由 main.py lifespan 创建并单持有；单例性由"只构造一次"保证，
    不再依赖类级 __new__ 单例与 stop 时自清 _instance。
    """

    def __init__(self):
        self._registry = ServiceRegistry()
        self.is_running = False

        # 初始化记忆数据库连接（SQLite，懒连接，无 IO）
        logger.info("开始初始化记忆数据库连接...")
        from brain.hc.database.core import get_memory_db
        self.memory_db = get_memory_db()
        logger.info("记忆数据库连接初始化完成")

        # 初始化消息处理器（同步，handler 本身无 IO）
        self.message_processor = get_message_handler()
        # 初始化注意力核心
        self.attention_core = get_attention_core()
        # AMY 杏仁核模块（start 时创建）
        self.amy_core = None

    async def start(self):
        """启动 SystemCore 及各服务。启动失败异常上抛给 lifespan。"""
        if self._registry.started:
            logger.warning("SystemCore已经处于运行状态")
            return

        # —— 按依赖顺序注册（stop 按注册逆序执行）——
        self._registry.register(FuncService("memory_db", stop=self._stop_memory_db))
        self._registry.register(FuncService("message_store", stop=self._stop_message_store))
        self._registry.register(FuncService(
            "default_initializer", start=self._init_defaults))
        self._registry.register(FuncService("amy", start=self._start_amy, stop=self._stop_amy))
        self._registry.register(FuncService(
            "attention",
            start=self.attention_core.start, stop=self.attention_core.stop))
        self._registry.register(FuncService(
            "message_handler",
            start=self.message_processor.start, stop=self.message_processor.stop))
        self._registry.register(FuncService(
            "channels", start=self._start_channels, stop=self._stop_channels))

        await self._registry.start_all()

        self.is_running = True
        logger.info("SystemCore初始化完成")

    async def stop(self):
        """停止 SystemCore 及各服务（registry 逆序；单项失败记录日志继续）。"""
        if not self.is_running and not self._registry.started:
            logger.warning("SystemCore已经处于停止状态")
            return

        await self._registry.stop_all()
        self.amy_core = None
        self.is_running = False
        logger.info("SystemCore已停止")

    async def __aenter__(self):
        """异步上下文管理器进入方法"""
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出方法"""
        await self.stop()
        if exc_type is not None:
            logger.error(f"SystemCore上下文退出时发生异常: {exc_val}")
        return False  # 不抑制异常

    # ============================================================
    # 各服务的启动/停止实现
    # ============================================================

    async def _init_defaults(self) -> None:
        """默认值预设：默认角色是后续人格 / impress / vector 等能力的前提，
        按 user_rules 关键方法失败应崩溃阻断——这里抛出异常，由 registry
        回滚后上抛 lifespan，禁止静默吞。"""
        from brain.hc.database.default_initializer import DefaultInitializer
        await DefaultInitializer.initialize_all()
        logger.info("默认值预设初始化完成")

    async def _start_amy(self) -> None:
        """启动 AMY（get_emotional_interface 首次创建时完成系统 act 引导）。"""
        self.amy_core = await get_emotional_interface()

    async def _stop_amy(self) -> None:
        """停止 AMY：清空单例（预设切换后单例可能已换新，
        从模块全局解析而非持有引用，确保清掉的是当前那个）。"""
        await reset_emotional_interface()
        self.amy_core = None

    async def _start_channels(self) -> None:
        """注册并启动 adapt channels（NapCat 反向 WS 服务端 / Web 前端）。

        channels 最后启动：先切断一切就绪后再接受外部事件流入。
        """
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

    async def _stop_channels(self) -> None:
        """停止所有 channel 并重置 manager 单例，确保下次 start 干净重建。"""
        mgr = await get_channel_manager()
        await mgr.stop_all()
        await reset_channel_manager()

    async def _stop_memory_db(self) -> None:
        logger.info("开始关闭记忆数据库连接...")
        await self.memory_db.close()
        logger.info("记忆数据库连接已关闭")

    async def _stop_message_store(self) -> None:
        """关闭消息仓库连接（aiosqlite 的 worker 线程是非 daemon 的，
        不关闭会导致进程退出时被卡住，Ctrl+C 无法释放终端）。"""
        from memory.message_repository import get_message_store
        await get_message_store().close()
