# -*- coding: utf-8 -*-
"""通用输入辅助方法

存放 conversation / social_chat 共用的输入构造辅助函数（纯函数，无实例依赖）。
"""


def build_session_environment(session_id: str, session_name: str, master_id: str) -> str:
    """构造【当前会话环境】段落。

    严格遵循项目规则：session_id 为空直接返回空串，不静默兜底到"未知"骗 LLM。

    Args:
        session_id: 会话 ID（格式 "type_id"）。
        session_name: 会话对应实体名称（角色名 / 群名）。可为空——为空时填"未知"。
        master_id: 主人 ID（用于判定是否主人私聊会话）。

    Returns:
        完整的环境段落字符串；session_id 为空时返回空串。
    """
    if not session_id:
        return ""

    is_private = session_id.startswith("private_")
    is_group = session_id.startswith("group_")
    is_master = is_private and session_id == f"private_{master_id}"

    name = session_name or "未知"
    lines = [f"【当前会话环境】你正在与{name} id {session_id} 交流"]

    if is_private:
        lines.append("这里是私人会话，你的对话仅会被对方注意到！")
    elif is_group:
        lines.append("这里是群聊会话，你的发言会被所有人注意到！")

    # 只要 session_id 不是主人的私聊会话（不论群聊还是其他人的私聊），
    # 都追加"与主人以外的其他人对话"提示，由 LLM 自行根据 type 决定如何处理。
    if not is_master:
        lines.append("你正在与主人以外的其他人对话！威严慎行，随时请求援助！")

    return "\n".join(lines)
