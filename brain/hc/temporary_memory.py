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


class TemporaryMemory:
    """临时记忆（单例由模块级 _temporary_memory 持有）。

    模块任务：串联下一个 LLM 循环——本循环写入关键事实，PFCA / proactive 下一轮
    组装首条输入时经 get_recent_experience 取用。详见 docs/api/temporary_memory.md。
    """

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

    def _get_timestamp(self) -> float:
        """获取当前时间戳"""
        import time
        return time.time()

    def get_formatted(self, limit: Optional[int] = DEFAULT_DISPLAY_LIMIT) -> str:
        """
        获取格式化的短期记忆文本（含时间戳）

        Args:
            limit: 最多返回多少条（默认 DEFAULT_DISPLAY_LIMIT）。None/<=0 表示全部。

        Returns:
            str: 多行文本，每行形如 "1. [2026-07-01 22:30:00] xxx"，最新一条在前。
        """
        if limit is None or limit <= 0 or limit >= len(self._messages):
            recent = self._messages
        else:
            recent = self._messages[-limit:]
        if not recent:
            return ""
        # 倒序输出：最近经历按最新在前排列，编号 1 为最新一条
        return "".join(
            _format_message_line(i, msg) + "\n"
            for i, msg in enumerate(reversed(recent), 1)
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
