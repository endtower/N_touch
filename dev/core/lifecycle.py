#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
lifecycle.py

统一生命周期基础设施：Service 协议 + ServiceRegistry。

约定：
- start/stop 必须幂等（重复调用安全）。
- start_all 按注册顺序执行；任一失败 → 逆序回滚已启动项后原样抛出
  （启动失败必须炸到调用方，由 lifespan 决定进程退出）。
- stop_all 逆序执行；单项失败记日志继续（关停阶段绝不互相阻断）。
- 组件本身不再自带 __new__ 单例：单例统一由各模块的 get_xxx() 工厂持有，
  需要挂进生命周期的组件用 FuncService 适配（start/stop/reset 均可选）。
"""

import asyncio
import inspect
from typing import Callable, List, Optional

from utils.logger import get_logger

logger = get_logger('lifecycle')


class Service:
    """生命周期组件基类。按需覆写 start/stop/reset。"""
    name: str = "service"

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def reset(self) -> None: ...      # 单例重置钩子（测试/热重载用），默认空


async def _call_maybe_async(func: Callable, *args) -> None:
    """调用同步或异步函数：async 返回值自动 await（存量组件 start/stop 有同步有异步）。"""
    result = func(*args)
    if inspect.isawaitable(result):
        await result


class FuncService(Service):
    """把既有 start/stop/close 函数适配成 Service（同步/异步函数均可）。"""

    def __init__(self, name: str,
                 start: Optional[Callable] = None,
                 stop: Optional[Callable] = None,
                 reset: Optional[Callable] = None):
        self.name = name
        self._start, self._stop, self._reset = start, stop, reset

    async def start(self) -> None:
        if self._start:
            await _call_maybe_async(self._start)

    async def stop(self) -> None:
        if self._stop:
            await _call_maybe_async(self._stop)

    async def reset(self) -> None:
        if self._reset:
            await _call_maybe_async(self._reset)


class ServiceRegistry:
    """按注册顺序启停的服务注册表。"""

    def __init__(self) -> None:
        self._services: List[Service] = []
        self._started: List[Service] = []
        self._lock = asyncio.Lock()

    def register(self, service: Service) -> None:
        if any(s.name == service.name for s in self._services):
            raise ValueError(f"重复注册 service: {service.name!r}")
        self._services.append(service)

    @property
    def started(self) -> bool:
        return bool(self._started)

    async def start_all(self) -> None:
        """按注册顺序启动。任一失败 → 逆序回滚已启动项后原样抛出。"""
        async with self._lock:
            for svc in self._services:
                try:
                    await svc.start()
                    self._started.append(svc)
                    logger.info(f"[lifecycle] 已启动: {svc.name}")
                except Exception:
                    logger.exception(f"[lifecycle] 启动 {svc.name} 失败，回滚已启动项")
                    await self._stop_started()
                    raise

    async def stop_all(self) -> None:
        """逆序停止已启动的服务；单项失败记录日志继续。"""
        async with self._lock:
            await self._stop_started()

    async def reset_all(self) -> None:
        """测试/热重载：逆序 stop + 逆序 reset 单例。"""
        await self.stop_all()
        for svc in reversed(self._services):
            try:
                await svc.reset()
            except Exception:
                logger.exception(f"[lifecycle] reset {svc.name} 失败")

    async def _stop_started(self) -> None:
        for svc in reversed(self._started):
            try:
                await svc.stop()
                logger.info(f"[lifecycle] 已停止: {svc.name}")
            except Exception:
                logger.exception(f"[lifecycle] 停止 {svc.name} 失败（继续其余项）")
        self._started.clear()
