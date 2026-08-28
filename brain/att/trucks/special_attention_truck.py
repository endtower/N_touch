"""
SpecialAttentionTruck - 特别关注群聊消息获取卡车

功能：
- 监控特别关注群聊的消息
- 与station进行通信
- 响应station的continue、pause、restart命令
- 触发条件满足后向station发送消息
"""

import asyncio
import time
from utils.logger import get_logger
from .base_truck import BaseTruck

logger = get_logger(__name__, "INFO")

class SpecialAttentionTruck(BaseTruck):
    """特别关注群聊消息卡车 - 支持station通信"""
    
    def __init__(self):
        super().__init__("special_attention_truck")
        self.target_user_id = None
        self.target_user_name = None
    
    async def _main_loop(self):
        """主循环 - 每隔5秒，检测有无需要处理的群聊会话，有则提醒station"""
        logger.info("SpecialAttentionTruck 已启动")  # 启动时输出 info 级别日志
        while self.state != "stopped":
            # 检测有无需要处理的群聊会话
            from memory.message_advanced import get_message_advanced_manager
            message_advanced_manager = get_message_advanced_manager()
            unread_sessions = await message_advanced_manager.get_unread_sessions_by_type("group")
            
            # 筛选出特别关注群聊的消息（检查最新20条消息中是否有目标用户）
            target_sessions = []
            if unread_sessions:
                for session in unread_sessions:
                    # 检查群聊的最新消息中是否包含特别关注用户
                    if await self._is_target_group_session(session):
                        target_sessions.append(session)
            
            # 只有当有特别关注群聊的未读会话时才提醒station
            if target_sessions:
                await self.remind_station()
            else:
                logger.debug("未获取到特别关注群聊的未读消息")
            await self.time_control()
    
    async def _is_target_group_session(self, session):
        """检查群聊会话的最新消息中是否包含特别关注用户"""
        from config.truck_settings import special_attention_truck_config
        from memory.message_repository import get_message_store
        
        target_user_id = special_attention_truck_config.TARGET_USER_ID
        session_id = session.get("session_id", "")
        
        if not session_id:
            return False
        
        message_repository = get_message_store()
        session_messages_result = await message_repository.get_latest_session_messages(
            session_id=session_id,
            session_type="group",
            n=20
        )
        
        # 检查这20条消息中是否有目标用户发送的消息
        message_list = session_messages_result.get("message_list", [])
        for msg in message_list:
            # 检查消息的发送者ID是否为目标用户
            msg_user_id = msg.get("user_id", "")
            if target_user_id in str(msg_user_id):
                return True
        
        return False
    
    async def time_control(self):
        """时间控制 - 控制truck的运行时间，支持暂停功能"""
        from config.truck_settings import special_attention_truck_config
        total_sleep_time = special_attention_truck_config.CYCLE_TIME
        await self.cycle_control(total_sleep_time)

    async def get_special_attention_messages(self):
        """实现从memory模块获取特别关注群聊消息的功能"""
        from memory.message_advanced import get_message_advanced_manager
        from memory.message_repository import get_message_store
        from config.truck_settings import special_attention_truck_config
        
        message_advanced_manager = get_message_advanced_manager()
        message_repository = get_message_store()
        
        unread_sessions = await message_advanced_manager.get_unread_sessions_by_type("group")
        
        target_sessions = []
        for session in unread_sessions:
            if await self._is_target_group_session(session):
                target_sessions.append(session)
        
        target_sessions.sort(key=lambda x: x.get('unread_count', 0), reverse=True)
        
        if target_sessions:
            session = target_sessions[0]
            session_id = session.get("session_id", "")
            if session_id:
                session_messages_result = await message_repository.get_latest_session_messages(
                    session_id=session_id,
                    session_type="group",
                    n=10
                )
                
                # 获取消息列表并按时间戳降序排序（最新的在前）
                message_list = session_messages_result.get("message_list", [])
                sorted_messages = sorted(message_list, key=lambda x: x.get("timestamp", 0), reverse=True)
                
                # 构建会话信息字典
                session_info = {
                    "session_id": session_id,
                    "session_type": "group",
                    "session_name": session.get("session_name", ""),
                    "remark": session.get("remark", ""),
                    "last_update_time": session.get("last_update_time", 0),
                    "message_count": session.get("message_count", 0),
                    "unread_count": session.get("unread_count", 0),
                    "message_list": sorted_messages
                }
                
                self.logger.info(f"获取到特别关注群聊的 1 个未读会话: {session_id}")
                self.logger.debug(f"特别关注群聊消息处理完成，未读消息数: {session_info.get('unread_count', 0)}")
                
                return session_info
        
        self.logger.debug("未获取到特别关注群聊的未读消息")
        return None
    
    async def format_special_attention_messages(self, message):
        """按照指定格式格式化特别关注群聊消息
        
        Args:
            message: 未读群聊会话对象
            
        Returns:
            str: 格式化后的消息
        """
        if not message:
            return "暂无特别关注群聊的未读消息"
        
        from config.truck_settings import special_attention_truck_config
        
        result_lines = []
        
        session = message
        session_id = session.get("session_id", "未知ID")
        target_user_id = special_attention_truck_config.TARGET_USER_ID
        
        # 获取消息列表并限制为最多10条，然后反转顺序使旧消息在前
        message_list = session.get("message_list", [])[:10]
        message_list.reverse()
        
        # 查找特别关注用户发送的第一条消息，获取其名称
        special_user_name = "未知用户"
        for msg in message_list:
            msg_user_id = str(msg.get("user_id", ""))
            if target_user_id in msg_user_id:
                special_user_name = msg.get("user_name", "未知用户")
                break
        
        # 添加第一行：提醒消息
        result_lines.append(f"在 群聊 {session_id} 中收到了来自特别关注 {special_user_name}（{target_user_id}）的消息")
        
        # 添加消息列表标题
        result_lines.append("[该会话最近10条消息]")
        
        # 直接使用完整日期时间格式，暂不使用相对时间
        for msg in message_list:
            timestamp = msg.get("timestamp", 0)
            if timestamp > 0:
                # 直接显示完整的日期时间格式
                full_datetime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
                
                # 获取发送者和内容
                sender = msg.get("user_name", "未知发送者")
                content = msg.get("content_text", "")
                
                # 检查消息发送者是否为目标用户
                msg_user_id = str(msg.get("user_id", ""))
                is_special_user = target_user_id in msg_user_id
                
                # 如果是特别关注用户的消息，添加标记
                if is_special_user:
                    result_lines.append(f"[特别关注对象] {full_datetime} {sender}：{content}")
                else:
                    result_lines.append(f"{full_datetime} {sender}：{content}")
        
        # 添加最后的选择提示
        result_lines.append("可以选择观察或参与或私聊。")
        
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
        unread_data = await self.get_special_attention_messages()
        
        # 如果没有获得unread_data，则直接返回None
        if not unread_data:
            return None
        
        formatted_message = await self.format_special_attention_messages(unread_data)
        
        return {
            "formatted_message": formatted_message,
            "raw_sessions": unread_data
        }
