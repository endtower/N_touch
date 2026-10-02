# -*- coding: utf-8 -*-

"""
消息处理器模块 (Message Handler)

此模块是系统的业务逻辑层，负责消息的业务逻辑处理、格式验证和流程协调。
主要任务包括：
1. 验证和标准化消息格式
2. 协调消息处理流程
3. 管理消息处理器的运行状态
4. 发布消息处理事件通知

链路统一使用 async/await：
    channel（OneBot/Web，经 ChannelManager 注入本处理器）
       -> message_processor.process_message (async)
       -> message_repository.store_message (async, aiosqlite)
    落库后由 att 的 trucks / 各 input_manager 通过游标轮询拉取未读消息驱动业务。

会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。

主要类与方法：

MessageHandler类：
- normalize_message_format: 将接收到的消息转换为标准格式并验证
  输入: raw_message (原始消息数据)
  输出: 标准化后的消息数据字典
  调用: 无

- start: 启动消息处理器
  输入: 无
  输出: 无
  调用: 无

- stop: 停止消息处理器
  输入: 无
  输出: 无
  调用: 无

- process_message: 异步处理接收到的消息，包括标准化、存储和发布通知
  输入: message (待处理的消息数据)
  输出: 无
  调用: normalize_message_format, _message_repository.store_message (来自memory.message_repository)

模块级别函数：
- get_message_handler: 获取消息处理器的单例实例
  输入: 无
  输出: MessageHandler实例
  调用: MessageHandler构造函数

引用其他模块：
- 从memory.message_repository导入get_message_store
- 从utils导入setup_logging, async_handle_errors, ValidationError
"""


from typing import Dict, Any

# 导入工具模块
from utils import setup_logging, async_handle_errors, ValidationError, MessageError

# 导入消息仓库模块
from memory.message_repository import get_message_store

# 设置日志
logger = setup_logging(__name__)


class MessageHandler:
    """消息处理器类，负责消息的业务逻辑处理、格式验证和流程协调

    这是系统的业务逻辑层，专注于消息的处理逻辑，包括消息格式标准化、
    消息处理流程协调、状态管理等，不直接与文件系统交互，而是通过
    MessageRepository来处理数据持久化。单例由模块级工厂持有。
    """

    def __init__(self):
        """初始化MessageHandler实例

        初始化处理器状态和依赖的服务。
        """
        # 防止重复初始化
        if not hasattr(self, '_initialized'):
            self.is_running = False
            # 获取消息仓库实例，用于数据持久化
            self._message_repository = get_message_store()
            # 输出合并后的初始化日志
            logger.info("memory系统初始化完毕")
            self._initialized = True

    async def ensure_store_ready(self) -> None:
        """启动钩子（SystemCore）：确保 chat_history 连接与表结构就绪。

        必须先于 attention/通道启动——否则并发首访会撞上未完成初始化的连接。
        """
        await self._message_repository.ensure_ready()

    def normalize_message_format(self, raw_message: Dict[str, Any]) -> Dict[str, Any]:
        """将接收到的消息转换为标准格式，并验证关键信息

        Args:
            raw_message: 接收到的原始消息

        Returns:
            标准化后的消息

        Raises:
            ValidationError: 消息格式验证失败
        """
        if not isinstance(raw_message, dict):
            raise ValidationError(f"消息格式错误: 期望dict类型，实际为{type(raw_message).__name__}")

        # 验证并提取必要的消息结构
        if "session_info" not in raw_message:
            raise ValidationError("消息格式错误: 缺少 'session_info' 字段")

        if "message_list" not in raw_message:
            raise ValidationError("消息格式错误: 缺少 'message_list' 字段")

        # 验证session_info格式
        session_info = raw_message["session_info"]
        if not isinstance(session_info, dict):
            raise ValidationError(f"session_info格式错误: 期望dict类型，实际为{type(session_info).__name__}")

        # 验证关键信息：会话ID（统一格式 "type_id"，如 "private_1234567890" / "group_198431299"）
        if "session_id" not in session_info or not session_info["session_id"]:
            raise ValidationError("消息格式错误: session_info中缺少有效的 'session_id' 字段")

        # 验证message_list格式
        message_list = raw_message["message_list"]
        if not isinstance(message_list, list):
            raise ValidationError(f"message_list格式错误: 期望list类型，实际为{type(message_list).__name__}")

        # 确保message_list不为空
        if not message_list:
            raise ValidationError("消息格式错误: message_list不能为空")

        # 验证列表中的每条消息
        latest_timestamp = 0
        for msg in message_list:
            if not isinstance(msg, dict):
                raise ValidationError(f"消息列表中包含非dict类型的元素: {type(msg).__name__}")

            # 确保必要的消息字段存在
            required_message_fields = ["message_id", "user_id", "timestamp", "content"]
            for field in required_message_fields:
                if field not in msg:
                    raise ValidationError(f"消息格式错误: 消息中缺少必要的 '{field}' 字段")

            # 验证content格式 - 现在是列表格式
            content = msg["content"]
            if not isinstance(content, list):
                raise ValidationError(f"content格式错误: 期望list类型，实际为{type(content).__name__}")

            # 验证content列表中的每个元素
            for content_item in content:
                if not isinstance(content_item, dict):
                    raise ValidationError(f"content项格式错误: 期望dict类型，实际为{type(content_item).__name__}")
                if "type" not in content_item:
                    raise ValidationError("消息格式错误: content项中缺少 'type' 字段")
                if "data" not in content_item:
                    raise ValidationError("消息格式错误: content项中缺少 'data' 字段")

            # 更新最新时间戳
            latest_timestamp = max(latest_timestamp, msg["timestamp"])

        # 返回标准化的消息格式
        # 只包含原始消息中存在的字段，避免覆盖现有会话信息
        session_info_normalized = {
            "session_id": str(session_info["session_id"]),  # 统一格式 "type_id"，如 "private_1234567890"
            "last_update_time": latest_timestamp  # 使用最新消息的时间戳
        }

        # 只有当原始消息中包含session_name时，才添加到标准化后的消息中
        if "session_name" in session_info:
            session_info_normalized["session_name"] = session_info["session_name"]

        # 只有当原始消息中包含remark时，才添加到标准化后的消息中
        if "remark" in session_info:
            session_info_normalized["remark"] = session_info["remark"]

        return {
            "session_info": session_info_normalized,
            "message_list": message_list
        }

    def start(self):
        """启动消息处理器

        启动消息处理器并设置运行状态。
        """
        if self.is_running:
            logger.warning("消息处理器已经处于运行状态")
            return

        self.is_running = True
        logger.info("消息处理器已启动")

    def stop(self):
        """停止消息处理器

        停止消息处理器并设置运行状态。
        """
        if not self.is_running:
            logger.warning("消息处理器已经处于停止状态")
            return

        self.is_running = False
        logger.info("消息处理器已停止")

    @async_handle_errors(error_types=(Exception,), raise_on=(Exception,), default_return=None)
    async def process_message(self, message: Dict[str, Any]):
        """异步处理接收到的消息，包括标准化、存储和发布通知

        全链路统一 async/await：channel 协程内直接 await 本方法（MessageBus 已删除）；
        内部直接 await store_message(...)。

        消息落库是数据完整性的关键路径（硬约定：重大关键方法失败必须崩溃阻断，
        防止静默丢消息）：本方法内任何异常都会记录日志后 os._exit(1) 终止进程
        （raise_on=(Exception,)），因此方法体内不再写 try/except。

        Args:
            message: 待处理的消息数据，包含session_info和message_list字段
        """
        if not self.is_running:
            # 消息处理器未运行还收到消息 = 启动/停止时序有误，直接崩溃阻断，
            # 绝不静默丢弃消息（KNOWN_ISSUES #11 教训）
            raise MessageError("消息处理器未运行，拒绝接收消息（时序错误，崩溃阻断）")

        # 提取消息基本信息（用于调试日志）
        message_list = message.get('message_list', [])
        if message_list and isinstance(message_list, list):
            first_message = message_list[0]
            message_id = first_message.get('message_id', 'unknown')
            user_id = first_message.get('user_id', 'unknown')
            logger.debug(f"消息处理器已接收消息: message_id={message_id}, user_id={user_id}")

        # 标准化 + 直接 await 异步存储
        normalized_message = self.normalize_message_format(message)
        await self._message_repository.store_message(normalized_message)
        logger.debug(f"消息已成功存储: message_id={normalized_message['message_list'][0].get('message_id', 'unknown')}")


_message_handler_instance: MessageHandler = None  # type: ignore[assignment]


def get_message_handler():
    """获取消息处理器的单例实例

    Returns:
        MessageHandler: 消息处理器的单例实例
    """
    global _message_handler_instance
    if _message_handler_instance is None:
        _message_handler_instance = MessageHandler()
    return _message_handler_instance