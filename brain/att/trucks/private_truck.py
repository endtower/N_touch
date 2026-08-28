
"""
PrivateTruck - 支持station通信版本的私聊消息获取卡车

功能：
- 定期从memory模块获取未读私聊消息
- 与station进行通信
- 响应station的continue、pause、restart命令
- 获取消息后向station发送
"""

import asyncio
import time
from typing import List, Dict, Any
from utils.logger import get_logger
from .base_truck import BaseTruck

logger = get_logger(__name__, 'INFO')

def _describe_duration(seconds: float) -> str:
    """把秒数格式化为相对时间描述（x年前/月前/天前/小时前/分钟前/秒前）。"""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}秒前"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}小时前"
    days = hours // 24
    if days < 30:
        return f"{days}天前"
    months = days // 30
    if months < 12:
        return f"{months}个月前"
    return f"{months // 12}年前"

class PrivateTruck(BaseTruck):
    """私聊消息卡车 - 支持station通信，实现私聊消息获取功能"""
    
    def __init__(self):
        super().__init__("private_truck")
    
    def start(self):
        """启动truck或从暂停恢复"""
        if self.state == "stopped":
            # 首次启动
            logger.info("PrivateTruck 已启动")  # 启动时输出 info 级别日志
            self.state = "running"
            self.pause_event.set()  # 确保事件处于非暂停状态
            self._task = asyncio.create_task(self._main_loop())
        elif self.state == "paused":
            self.state = "running"
            self.pause_event.set()
            logger.info("PrivateTruck已恢复运行")

    async def _main_loop(self):
        """主循环 - 每隔指定时间，检测有无需要处理的会话，有则提醒station"""
        while self.state != "stopped":
            # 仅检测有无需要处理的会话，不获取完整消息
            from memory.message_advanced import get_message_advanced_manager
            message_advanced_manager = get_message_advanced_manager()
            unread_sessions = await message_advanced_manager.get_unread_sessions_by_type("private")

            if unread_sessions:
                await self.remind_station()
            await self.cycle_control()
                
    async def cycle_control(self):
        """循环控制 - 控制truck的运行时间，支持暂停功能"""
        from config.truck_settings import private_truck_config
        total_sleep_time = private_truck_config.CYCLE_TIME
        await super().cycle_control(total_sleep_time)

    async def get_unread_private_messages(self):
        """实现从memory模块获取未读私聊消息的功能"""
        # 导入memory模块中的必要组件
        from memory.message_advanced import get_message_advanced_manager
        
        # 获取消息高级管理器
        message_advanced_manager = get_message_advanced_manager()
        
        # 获取所有未读的私聊会话
        unread_sessions = await message_advanced_manager.get_unread_sessions_by_type("private")
        
        # 按照未读消息数目降序排序
        unread_sessions.sort(key=lambda x: x.get('unread_count', 0), reverse=True)
        
        # 只处理第一个会话（最多一个）
        if unread_sessions:
            session = unread_sessions[0]
            session_id = session.get("session_id", "")
            if session_id:
                session_info = {
                    "session_id": session_id,
                    "session_name": session.get("session_name", ""),
                    "unread_count": session.get("unread_count", 0)
                }
                logger.debug(f"获取到 1 个未读私聊会话: {session_id}, 未读数: {session_info.get('unread_count', 0)}")
                return session_info
        
        return None
    
    async def format_private_messages(self, message):
        """按照指定格式格式化私聊消息

        触发时展示最新未读消息（最多 3 条），并提示会话未读总数；
        触发后把 pfca 游标推进到会话最新消息，消费闭环由 truck 侧完成，
        避免后续轮询对同一会话重复触发。

        Args:
            message: 未读私聊会话对象

        Returns:
            str: 格式化后的消息
        """
        if not message:
            return "暂无未读私聊消息"

        session = message
        session_id = session.get("session_id", "")
        session_name = session.get("session_name", "未知用户")

        from memory.message_repository import get_message_store
        from memory.session_history import (
            get_global_session_history,
            FetchOptions,
            MessageFormatOptions,
        )

        # 用单例（测试隔离库/真实库），勿新建默认路径实例
        store = get_message_store()

        # 先统计再推进：统计的是"游标未动"时的未读【用户消息】数
        unread_count = await store.count_unread_user_for(session_id, "pfca")

        result_lines = [f"你收到了来自{session_name}的新消息。"]

        # 展示最新未读消息，最多 3 条（格式："[新] [8月24号 10:52:45] 主人:在？"）。
        # newest=True 取全局最新的原始未读（上限 MAX_UNREAD_FETCH_LIMIT=50），
        # 过滤 system 后取最新的 3 条用户消息，保证展示的一定是最新用户消息。
        MAX_SHOW_NEW = 3
        if unread_count > 0:
            unread_raw = await store.get_unread_for(session_id, "pfca", newest=True)
            unread_user = [m for m in unread_raw if m.get("user_id") != "system"]
            show_messages = unread_user[-MAX_SHOW_NEW:]
            history = get_global_session_history()
            show_lines, _ = history.format_messages(
                show_messages,
                FetchOptions(
                    message_format=MessageFormatOptions(
                        show_new_marker=True,
                        show_timestamp=True,
                    )
                ),
            )
            result_lines.extend(show_lines)

        result_lines.append(f"目标会话共中存在{unread_count}条未读消息。")

        # 上次处理该会话的时间：取移动前游标位置对应消息的时间，计算其与现在的差
        current_cursor = await store.get_cursor(session_id, "pfca")
        if current_cursor >= 0:
            prev_messages = await store.get_messages_before_cursor(session_id, "pfca", 1)
            if prev_messages:
                last_ts = prev_messages[-1].get("timestamp", 0)
                if last_ts > 0:
                    result_lines.append(f"上次处理该会话为{_describe_duration(time.time() - last_ts)}")

        # 触发后把 pfca 游标推进到会话最新消息（只进不退）
        latest = await store.get_latest_session_messages(session_id, 1)
        latest_messages = latest.get("message_list", [])
        if latest_messages:
            latest_seq = latest_messages[0]["_seq"]
            if latest_seq > current_cursor:
                await store.advance_cursor(session_id, "pfca", latest_seq)
                logger.info(f"PrivateTruck 触发后推进 pfca 游标 {current_cursor} -> {latest_seq} ({session_id})")

        return "\n".join(result_lines)




        
    # 使用基类的remind_station方法
    
    async def get_truck_messages(self):
        """获取truck的消息，包括格式化后的消息和原始会话数据
        
        Returns:
            Dict: 包含格式化消息和原始会话数据的字典
                - formatted_message: str 格式化后的消息
                - raw_sessions: Dict 原始会话数据
            None: 如果没有需要处理的消息
        """
        unread_data = await self.get_unread_private_messages()
        
        # 如果没有获得unread_data，则直接返回None
        if not unread_data:
            return None
        
        formatted_message = await self.format_private_messages(unread_data)
        
        return {
            "formatted_message": formatted_message,
            "raw_sessions": unread_data
        }

  
