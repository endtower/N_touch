"""
BaseTruck - 卡车基类，提供通用的状态管理和控制功能

功能：
- 通用的状态管理（运行、暂停、停止）
- 异步任务控制
- 循环控制
- 与station通信
"""

import asyncio
import time
from utils.logger import get_logger

class BaseTruck:
    """卡车基类 - 提供通用的状态管理和控制功能"""
    
    def __init__(self, truck_id):
        # 初始化日志记录器
        self.logger = get_logger(truck_id, "INFO")
        self.truck_id = truck_id
        self.state = "stopped"  # 使用单一状态变量
        self._task = None
        self.pause_time = 0  # 暂停时的时间戳
        self.pause_event = asyncio.Event()  # 用于暂停控制的事件
        self.pause_event.set()  # 初始状态为非暂停，设置事件
    
    def start(self):
        """启动truck或从暂停恢复"""
        if self.state == "stopped":
            # 首次启动
            self.logger.info(f"{self.truck_id} 已启动")  # 启动时输出 info 级别日志
            self.state = "running"
            self.pause_event.set()  # 确保事件处于非暂停状态
            self._task = asyncio.create_task(self._main_loop())
        elif self.state == "paused":
            # 从暂停恢复
            self.state = "running"
            self.pause_event.set()  # 恢复运行，设置事件
            self.logger.info(f"{self.truck_id}已恢复运行")
    
    def pause(self):
        """暂停truck - 冻结时间计时"""
        if self.state == "running":
            self.state = "paused"
            self.pause_time = time.time()
            self.pause_event.clear()
            self.logger.debug(f"{self.truck_id}已暂停")
    
    def stop(self):
        """停止truck"""
        self.logger.debug(f"{self.truck_id}已停止")
        self.state = "stopped"
        self.pause_event.set()  # 停止时重置事件状态
        if self._task:
            self._task.cancel()

    async def cycle_control(self, cycle_time):
        """循环控制 - 控制truck的运行时间，支持暂停功能
        
        Args:
            cycle_time: 循环时间（秒）
        """
        total_sleep_time = cycle_time
        sleep_chunk = 0.5  # 每次睡眠的片段长度
        remaining_sleep = total_sleep_time
        
        while remaining_sleep > 0:
            # 检查暂停状态
            await self.pause_event.wait()
            
            # 睡眠当前片段
            current_sleep = min(sleep_chunk, remaining_sleep)
            await asyncio.sleep(current_sleep)
            remaining_sleep -= current_sleep

    async def remind_station(self):
        """向station发送提醒，告知需要处理任务，并暂停自身

        关键修复（#1）：
        - 先 pause 自己，再 await station_input。
          原顺序是先 await 入队、再 pause，存在窗口期让 truck 自身再次入队，
          在主 loop 拥堵时会把 task_queue 灌爆。
        - station 在处理完 (truck.get_truck_messages + 可能的 PFCA) 后，会调用
          truck.start() 把它从 paused 恢复到 running。
        """
        # 延迟导入以避免循环依赖
        from brain.att.station import station_input

        message = {
            "truck_id": self.truck_id  # 只包含truck_id，告知station需要处理该truck的任务
        }
        # 先暂停自身，避免在 station 取出任务前 _main_loop 又触发下一轮入队
        self.pause()
        await station_input(message)
        self.logger.debug("已向station发送处理提醒，等待station处理完成...")

    async def _main_loop(self):
        """主循环 - 子类需要实现"""
        raise NotImplementedError("子类必须实现_main_loop方法")
