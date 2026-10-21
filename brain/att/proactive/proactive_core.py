# -*- coding: utf-8 -*-
"""
proactive 模块核心

类比 pfca_core：
- 单例 ProactiveCore，对外暴露 get_proactive_core()
- 维护 running / should_stop 等生命周期标志
- start() 启动一个长期 task，负责消费主动任务队列
- stop() 通知内部 session 退出
"""

import asyncio
from typing import Any, Dict, Optional

from utils.error_handling import async_handle_errors
from utils.logger import get_logger

logger = get_logger("ProactiveCore", "INFO")


class ProactiveCore:
    """主动任务核心 - 负责主动任务的总调度与生命周期管理"""

    def __init__(self):
        logger.info("初始化 ProactiveCore")
        self.is_running: bool = False
        self.should_stop: bool = False
        self.current_task: Optional[asyncio.Task] = None
        self._main_task: Optional[asyncio.Task] = None
        # 当前正在执行的单次 proactive 会话
        self.current_session: Optional["ProactiveSession"] = None  # noqa: F821
        # 待消费的主动任务队列（driver / truck 投递，session 消费）
        self.task_queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()

    @async_handle_errors()
    async def start(self) -> None:
        """启动 ProactiveCore 主循环

        作为长期运行的 task 跑在与 att/pfca 共享的主 event loop 上。
        持续从 task_queue 取出 ProactiveTask 草稿，驱动一次 ProactiveSession。
        """
        if self.is_running:
            logger.debug("ProactiveCore 已在运行，跳过 start")
            return

        self.is_running = True
        self.should_stop = False
        logger.info("ProactiveCore 启动")
        self._main_task = asyncio.create_task(self._main_loop(), name="proactive-main-loop")

    @async_handle_errors()
    async def stop(self) -> None:
        """停止 ProactiveCore"""
        logger.info("ProactiveCore 停止中")
        self.should_stop = True
        self.is_running = False
        if self.current_session is not None:
            await self.current_session.stop()
            self.current_session = None
        if self._main_task is not None and not self._main_task.done():
            self._main_task.cancel()
            try:
                await self._main_task
            except (asyncio.CancelledError, Exception):
                pass
        self._main_task = None
        logger.info("ProactiveCore 已停止")

    async def _main_loop(self) -> None:
        """主循环：从 task_queue 取任务 -> 启动 ProactiveSession -> 等待结束"""
        logger.debug("ProactiveCore 主循环进入")
        while not self.should_stop:
            try:
                task_draft = await self.task_queue.get()
            except asyncio.CancelledError:
                break

            logger.info(f"ProactiveCore 收到一个主动任务草稿: intent={task_draft.get('intent', '?')}")
            session = None
            chain_token = None
            try:
                # 导入和构造也会失败，必须纳入同一个完成通知边界。
                from .proactive_session import ProactiveSession
                from utils.chain_context import set_chain_context, reset_chain_context
                chain_id = task_draft.get("chain_id")
                chain_token = set_chain_context(chain_id) if chain_id else None
                session = ProactiveSession(task_draft)
                self.current_session = session
                await session.start()
            except asyncio.CancelledError:
                task_draft["_error"] = RuntimeError("ProactiveCore 会话被取消")
                raise
            except Exception as e:
                task_draft["_error"] = e
                logger.exception("ProactiveSession 初始化或执行失败")
            finally:
                if chain_token is not None:
                    reset_chain_context(chain_token)
                self.current_session = None
                self.task_queue.task_done()
                # 回传决策结果给等待方（truck / station）
                # decision_result 是 ProactiveSession 在 process_cycle 里设置的:
                #   - 调 launch_pfca → {intent, tool_name='launch_pfca', tool_result}
                #   - 调 skip_proactive → {intent, tool_name='skip_proactive', skipped=True}
                #   - 没调任何终结工具 / 异常 → None
                task_draft["_decision_result"] = getattr(session, "decision_result", None)
                # 通知外部（truck / station）本次 session 已结束
                completion = task_draft.get("_completion_event")
                if completion is not None:
                    completion.set()
            logger.debug("ProactiveCore 完成一次任务, 等待下一个")

    async def feed_proactive_task(self, task_draft: Dict[str, Any]) -> None:
        """外部（driver / station / truck）投递一个主动任务草稿

        关键设计：投递后 await 到对应 ProactiveSession 跑完才返回。
        这样 truck.get_truck_messages() 的 await 会自然阻塞 station 不去
        立刻 truck.start()，避免 proactive_session 还没跑完 truck 就被恢复
        —— 恢复后下一轮 select 同一事件会再次触发。
        """
        worker = self._main_task
        if not self.is_running or worker is None or worker.done():
            raise RuntimeError("ProactiveCore 主循环未运行，无法处理主动任务")
        # 给 task 加一个完成事件, main_loop 处理完会 set 它
        completion = asyncio.Event()
        task_draft["_completion_event"] = completion
        await self.task_queue.put(task_draft)
        # 同时观察消费者存活；任务在出队前被取消也不能永久挂住 station。
        waiter = asyncio.create_task(completion.wait())
        try:
            await asyncio.wait((worker, waiter), return_when=asyncio.FIRST_COMPLETED)
            if not completion.is_set():
                if not worker.cancelled():
                    worker.result()
                raise RuntimeError("ProactiveCore 主循环已停止，主动任务未完成")
            error = task_draft.get("_error")
            if error is not None:
                raise RuntimeError("ProactiveCore 处理主动任务失败") from error
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        logger.info(f"已投递主动任务草稿, 当前队列长度: {self.task_queue.qsize()}")

    def is_busy(self) -> bool:
        """是否正在执行一个 session"""
        return self.current_session is not None

    async def wait_until_idle(self, timeout: Optional[float] = None) -> bool:
        """等待直到没有 session 在跑
        Returns:
            bool: True 表示已进入 idle；False 表示超时
        """
        start = asyncio.get_event_loop().time()
        while self.is_busy():
            if timeout is not None and asyncio.get_event_loop().time() - start > timeout:
                return False
            await asyncio.sleep(0.1)
        return True


# 全局单例
_global_proactive_instance: Optional[ProactiveCore] = None


def get_proactive_core() -> ProactiveCore:
    """获取 ProactiveCore 单例实例"""
    global _global_proactive_instance
    if _global_proactive_instance is None:
        _global_proactive_instance = ProactiveCore()
    return _global_proactive_instance
