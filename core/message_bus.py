#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
消息总线模块 - 实现了系统内部模块间的通信机制（全异步实现）

实现了发布/订阅模式，支持消息的广播和定向发送，确保消息的可靠传递和顺序处理。

全链路统一 async/await：
- 队列使用 asyncio.Queue（而非 threading.Queue）
- 调度任务直接以 asyncio.Task 跑在调用方所在的事件循环上，不再起独立工作线程
- subscribe 是元数据操作保持同步；publish / start / stop 全部为 async
- 回调兼容同步函数与 async 函数：async 回调由当前 loop 直接 await

调用方约定：必须在已经运行的 asyncio loop 内 await start() / publish() / stop()。
"""

import asyncio
import inspect
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List, Optional

from utils import AbyssCoreError
from utils.logger import get_logger

# 获取日志记录器
logger = get_logger('MessageBus')


class MessageBus:
    """消息总线实现（纯异步）"""

    def __init__(self) -> None:
        # 订阅关系 {topic: [callback]}
        self.subscribers: Dict[str, List[Callable[[Any], Any]]] = defaultdict(list)
        # 异步锁：保护订阅关系的并发修改
        self._subs_lock = asyncio.Lock()
        # 消息队列（启动时创建）
        self._queue: Optional[asyncio.Queue] = None
        # 后台处理任务
        self._worker_task: Optional[asyncio.Task] = None
        self.running = False

    async def start(self) -> None:
        """启动消息总线（在已运行的 asyncio loop 中调用）"""
        if self.running:
            return
        self.running = True
        self._queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._process_messages())
        logger.info("消息总线已启动")

    async def stop(self) -> None:
        """停止消息总线"""
        if not self.running:
            return
        self.running = False
        # 投递哨兵让 _process_messages 自然退出
        if self._queue is not None:
            await self._queue.put((None, None))
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker_task.cancel()
        logger.info("消息总线已停止")

    async def subscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        """订阅主题（异步，加锁保护并发修改）

        Args:
            topic: 消息主题
            callback: 同步或异步回调（async 回调会被自动 await）
        """
        async with self._subs_lock:
            self.subscribers[topic].append(callback)
        logger.info(
            f"已订阅主题: {topic}, 回调函数: {callback.__name__}, "
            f"当前订阅者数量: {len(self.subscribers[topic])}"
        )

    async def unsubscribe(self, topic: str, callback: Callable[[Any], Any]) -> None:
        """取消订阅（异步，加锁保护并发修改）"""
        async with self._subs_lock:
            if topic in self.subscribers and callback in self.subscribers[topic]:
                self.subscribers[topic].remove(callback)
                logger.info(f"已取消订阅主题: {topic}")

    async def publish(self, topic: str, message: Any) -> None:
        """发布消息到指定主题（异步）"""
        if self._queue is None:
            raise RuntimeError("MessageBus 未启动，请先 await start()")
        logger.info(f"发布消息到主题: {topic}")
        await self._queue.put((topic, message))

    async def _process_messages(self) -> None:
        """处理队列中的消息（在 asyncio loop 内运行的单一协程）"""
        assert self._queue is not None
        while True:
            topic, message = await self._queue.get()
            try:
                # 收到停止哨兵
                if topic is None and message is None:
                    break

                # 拷贝订阅者列表，避免回调中修改订阅关系
                async with self._subs_lock:
                    callbacks = list(self.subscribers.get(topic, []))

                logger.info(f"开始处理主题 {topic} 的消息，共有 {len(callbacks)} 个订阅者")
                for callback in callbacks:
                    try:
                        logger.info(f"调用回调函数: {callback.__name__}")
                        result = callback(message)
                        if inspect.isawaitable(result):
                            await result  # type: ignore[arg-type]
                        logger.info(f"回调函数 {callback.__name__} 调用成功")
                    except AbyssCoreError:
                        # 关键方法失败，崩溃阻断，防止错误扩大蔓延
                        raise
                    except Exception as e:
                        # 可恢复错误（如单个 channel 的消息处理失败），记录后继续
                        logger.error(f"处理消息时发生错误: {str(e)}")
            finally:
                self._queue.task_done()

    # 异步上下文管理器支持
    async def __aenter__(self) -> "MessageBus":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        await self.stop()
        return False


# 全局消息总线实例
_message_bus_instance: Optional[MessageBus] = None
_message_bus_lock = asyncio.Lock()


async def get_message_bus() -> MessageBus:
    """获取消息总线的单例实例（异步，必须在已运行的 loop 内调用）"""
    global _message_bus_instance
    async with _message_bus_lock:
        if _message_bus_instance is None:
            _message_bus_instance = MessageBus()
    return _message_bus_instance


async def reset_message_bus() -> None:
    """重置 MessageBus 单例，确保下次启动是干净状态"""
    global _message_bus_instance
    async with _message_bus_lock:
        _message_bus_instance = None