from typing import List, Dict, Any, Optional
from datetime import datetime

from utils.logger import get_logger

logger = get_logger('TemporaryMemory', 'WARNING')


# 默认上限 10（之前是 3）
MAX_MESSAGES = 10
# 默认显示最新 3 条（之前是全部）
DEFAULT_DISPLAY_LIMIT = 3


def _format_ts(ts: float) -> str:
    """将时间戳格式化为可读字符串"""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未知时间"


def _format_message_line(idx: int, msg: Dict[str, Any]) -> str:
    """格式化单条消息为可读文本（含时间戳）"""
    content = msg.get("content", "")
    ts = msg.get("timestamp")
    if ts:
        return f"{idx}. [{_format_ts(ts)}] {content}"
    return f"{idx}. {content}"


def _notify_dynamic_prompt(limit: int = DEFAULT_DISPLAY_LIMIT) -> None:
    """将当前临时记忆（最近 N 条）推送到 dynamic_prompt_manager.hc_temporary_memory

    Args:
        limit: 推送最近 N 条，默认 3。设置 <= 0 表示全部。
    """
    try:
        from utils.dynamic_prompt_manager import dynamic_prompt_manager
        # 按时间倒序取最近 N 条，再按原顺序（时间正序）格式化
        if limit is None or limit <= 0 or limit >= len(_temporary_memory._messages):
            recent = _temporary_memory._messages
        else:
            recent = _temporary_memory._messages[-limit:]
        if recent:
            value = "".join(
                _format_message_line(i, msg) + "\n"
                for i, msg in enumerate(recent, 1)
            )
        else:
            value = ""
        dynamic_prompt_manager.set('hc_temporary_memory', value)
    except Exception as e:
        logger.warning(f"推送 hc_temporary_memory 失败: {e}")


class TemporaryMemory:
    """临时记忆（单例由模块级 _temporary_memory 持有）。"""

    def __init__(self):
        if not hasattr(self, '_initialized'):
            # 存储结构：包含特殊标识的消息字典列表
            # 格式: {"identifier": "特殊标识", "content": "内容", "metadata": {}, "timestamp": float}
            self._messages: List[Dict[str, Any]] = []
            self._initialized = True

    def add(self, content: Any, identifier: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> None:
        """
        添加消息到临时内存

        Args:
            content: 消息内容
            identifier: 特殊标识（用于去重，如角色UUID）
            metadata: 附加元数据
        """
        # 创建消息对象
        message = {
            "identifier": identifier,
            "content": content,
            "metadata": metadata or {},
            "timestamp": self._get_timestamp()
        }

        # 记录添加的消息
        logger.info(f"添加到临时内存: content={content}, identifier={identifier}, metadata={metadata}")

        # 如果提供了特殊标识，检查是否存在相同标识的消息
        if identifier:
            # 找到并删除相同标识的旧消息
            to_remove = []
            for i, msg in enumerate(self._messages):
                if msg.get("identifier") == identifier:
                    to_remove.append(i)

            # 删除旧消息（从后往前删除，避免索引错乱）
            for i in sorted(to_remove, reverse=True):
                self._messages.pop(i)

        # 添加新消息
        self._messages.append(message)

        # 控制临时记忆上限为 MAX_MESSAGES 条，超过则删除最旧的一条（FIFO）
        if len(self._messages) > MAX_MESSAGES:
            removed_message = self._messages.pop(0)
            logger.warning(f"临时内存超过上限 ({MAX_MESSAGES}), 删除最旧消息: {removed_message['content']}")

        # 推送到 dynamic_prompt_manager.hc_temporary_memory（默认显示最新 3 条）
        _notify_dynamic_prompt(limit=DEFAULT_DISPLAY_LIMIT)

    def _get_timestamp(self) -> float:
        """获取当前时间戳"""
        import time
        return time.time()

    def clear(self) -> None:
        self._messages.clear()
        # 推送到 dynamic_prompt_manager.hc_temporary_memory（此时为空字符串）
        _notify_dynamic_prompt(limit=DEFAULT_DISPLAY_LIMIT)

    def get_messages(self, limit: Optional[int] = DEFAULT_DISPLAY_LIMIT) -> List[Any]:
        """
        获取临时消息的内容（按时间正序）

        Args:
            limit: 最多返回多少条
                - None 或 <= 0：全部
                - 正整数：最新的 N 条
                - 默认 DEFAULT_DISPLAY_LIMIT (3)

        Returns:
            List[Any]: 消息内容列表
        """
        if limit is None or limit <= 0 or limit >= len(self._messages):
            msgs = [msg["content"] for msg in self._messages]
        else:
            msgs = [msg["content"] for msg in self._messages[-limit:]]
        logger.debug(f"获取临时内存内容 (limit={limit}): {msgs}")
        return msgs

    def get_last_message(self) -> Any:
        """
        获取最后一条临时消息的内容

        Returns:
            Any: 最后一条临时消息的内容，如果没有则返回None
        """
        return self._messages[-1]["content"] if self._messages else None

    def get_raw_messages(self) -> List[Dict[str, Any]]:
        """
        获取原始消息对象（包含所有信息）

        Returns:
            List[Dict[str, Any]]: 原始消息对象列表
        """
        return self._messages

    def get_formatted(self, limit: Optional[int] = DEFAULT_DISPLAY_LIMIT) -> str:
        """
        获取格式化的短期记忆文本（含时间戳）

        Args:
            limit: 最多返回多少条（默认 DEFAULT_DISPLAY_LIMIT）。None/<=0 表示全部。

        Returns:
            str: 多行文本，每行形如 "1. [2026-07-01 22:30:00] xxx"
        """
        if limit is None or limit <= 0 or limit >= len(self._messages):
            recent = self._messages
        else:
            recent = self._messages[-limit:]
        if not recent:
            return ""
        return "".join(
            _format_message_line(i, msg) + "\n"
            for i, msg in enumerate(recent, 1)
        )

    def get_formatted_without_identifier(self, limit: Optional[int] = DEFAULT_DISPLAY_LIMIT) -> str:
        """获取仅含 identifier 为 None 的短期记忆文本（含时间戳）。

        设计动机：
            - 带 identifier 的消息通常与"按角色去重"的强语义记忆绑定，
              不适合混入面向 LLM 的"短期上下文"展示。
            - 一些调用方（如 social_agent 首条 user 消息）只想拿到
              普通的临时事件 / 上下文片段，因此提供本过滤接口。

        Args:
            limit: 最多返回多少条（默认 DEFAULT_DISPLAY_LIMIT）。
                None / <=0 表示全部；正整数表示"过滤后"的最新 N 条。

        Returns:
            str: 多行文本，每行形如 "1. [2026-07-01 22:30:00] xxx"
        """
        filtered = [m for m in self._messages if m.get("identifier") is None]
        if limit is None or limit <= 0 or limit >= len(filtered):
            recent = filtered
        else:
            recent = filtered[-limit:]
        if not recent:
            return ""
        return "".join(
            _format_message_line(i, msg) + "\n"
            for i, msg in enumerate(recent, 1)
        )


_temporary_memory = TemporaryMemory()


def get_temporary_memory() -> TemporaryMemory:
    return _temporary_memory


def get_recent_experience(
    limit: int = DEFAULT_DISPLAY_LIMIT,
    label: str = "近期经历",
    with_current_time: bool = True,
) -> str:
    """统一的"近期经历"获取接口，供 PFCA / proactive 等模块直接使用

    返回一段完整可读字符串，结构形如：
        【近期经历】(当前时间: 2026-07-02 11:33:09)
        1. [2026-07-02 11:33:08] xxx
        2. [2026-07-02 11:33:08] yyy

    Args:
        limit: 拉取最近 N 条，默认 DEFAULT_DISPLAY_LIMIT (3)
        label: 标签文本（PFCA 默认"近期经历"，proactive 可传"近期事件"等）
        with_current_time: 是否在标题后追加 (当前时间: ...)

    Returns:
        str: 完整可读字符串。无内容时返回 ""
    """
    if not _temporary_memory._messages:
        return ""
    body = _temporary_memory.get_formatted(limit=limit).rstrip()
    if not body:
        return ""
    header = f"【{label}】"
    if with_current_time:
        from datetime import datetime
        header += f"(当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})"
    return f"{header}\n{body}"


def get_recent_temporary_memory(limit: int = DEFAULT_DISPLAY_LIMIT, formatted: bool = True) -> Any:
    """便捷获取最近 N 条短期记忆

    Args:
        limit: 最多返回多少条，默认 DEFAULT_DISPLAY_LIMIT (3)
        formatted: True 返回格式化字符串（含时间戳），
                   False 返回 list[原始 message dict]

    Returns:
        str 或 list[dict]
    """
    if formatted:
        return _temporary_memory.get_formatted(limit=limit)
    return _temporary_memory.get_raw_messages()[-limit:] if limit and limit > 0 else _temporary_memory.get_raw_messages()