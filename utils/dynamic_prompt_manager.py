"""动态提示词中转层

仅做"中转"作用：把常用的动态提示词统一到一个读写入口，避免各模块各自维护。
不承担任何计算 / 生命周期 / 派生逻辑（那些逻辑已移除，见 docs/dynamic_prompt_guide.md）。

- 常量键（role_info / role_name / master_name）：直接从 role_config 读取，不缓存。
- 中转键（amy_effect / chat_environment / hc_temporary_memory / ...）：由各模块 set 写入、get 读取。
"""
from utils.logger import get_logger
from config.role_settings import role_config

logger = get_logger(__name__, "INFO")


class DynamicPromptManager:
    """动态提示词中转层"""

    def __init__(self):
        """初始化"""
        self._data = {}

    def set(self, key: str, value) -> None:
        """写入中转键的值"""
        self._data[key] = value

    def get(self, key: str, default=''):
        """读取：常量键直接从 role_config 读取，其余返回中转缓存。"""
        if key == 'role_info':
            return role_config.ASSISTANT_NAME_DESCRIPTION
        if key == 'role_name':
            return role_config.ASSISTANT_NAME
        if key == 'master_name':
            return role_config.MASTER_NAME
        return self._data.get(key, default)


dynamic_prompt_manager = DynamicPromptManager()


def get_dynamic_prompt(prompt_type: str, value=None):
    """获取或更新动态提示词（中转入口）。

    Args:
        prompt_type: 提示词类型
        value: 非空时执行更新（写入中转缓存）

    Returns:
        提示词的值，或者更新操作的结果
    """
    if value is not None:
        dynamic_prompt_manager.set(prompt_type, value)
        return True
    return dynamic_prompt_manager.get(prompt_type)
