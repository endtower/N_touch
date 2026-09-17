# -*- coding: utf-8 -*-
"""通用输入辅助方法

存放 conversation / social_chat 共用的输入构造辅助函数（纯函数，无实例依赖）。
"""

# conversation 输入消费的统一消费者游标（memory.session_history cursors 表，per session_id 隔离）。
# conversation 与 social_chat 读的是同一批会话数据（同一 session_id 的两种执行模式），
# 共用同一游标保证消费进度一致：任一模式消费过的消息，另一模式不再重复拉取。
CONVERSATION_CONSUMER_ID = "conversation"


def build_session_environment(
    session_id: str,
    session_name: str,
    master_id: str,
    include_non_master_warning: bool = True,
) -> str:
    """构造【当前会话环境】段落。

    严格遵循项目规则：session_id 为空直接返回空串，不静默兜底到"未知"骗 LLM。

    Args:
        session_id: 会话 ID（格式 "type_id"）。
        session_name: 会话对应实体名称（角色名 / 群名）。可为空——为空时填"未知"。
        master_id: 主人 ID（用于判定是否主人私聊会话）。
        include_non_master_warning: 是否追加"与主人以外的其他人对话"警示行
            （默认开启；social_chat 经参数关闭，该模块的监管已内建于发送通道）。

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
    # 都可追加"与主人以外的其他人对话"提示，由 LLM 自行根据 type 决定如何处理；
    # 经开关可关闭（social_chat 关闭——监管内建于 send_to_target 发送通道）。
    if not is_master and include_non_master_warning:
        lines.append("你正在与主人以外的其他人对话！威严慎行，随时请求援助！")

    return "\n".join(lines)


def build_social_chat_environment(
    master_session_id: str,
    master_name: str,
    working_session_id: str,
    working_session_name: str,
) -> str:
    """构造 social_chat 专用的合并单段【当前会话环境】。

    social_chat 同时服务两条会话线（主人会话 + 工作会话）。若逐线各调一次
    build_session_environment，首条 user 消息会出现两个【当前会话环境】段头；
    本 helper 合并为单段，如：
        【当前会话环境】你当前正在与{主人名}沟通（主人会话 id {master_session_id}），
        工作会话为{工作会话名} id {working_session_id}

    严格遵循项目规则：working_session_id 为空直接返回空串，不静默兜底"未知"骗 LLM；
    名字缺失按"未知"显式标注。不追加可见性行（群聊 / 私聊说明）——social_chat 的
    隐私语境已由合并句式自明，非主人警示同样不出现（监管内建于 send_to_target 发送通道）。

    Returns:
        完整的环境段落字符串；working_session_id 为空时返回空串。
    """
    if not working_session_id:
        return ""

    master_part = f"{master_name or '未知'}"
    if master_session_id:
        master_part += f"（主人会话 id {master_session_id}）"

    return (
        f"【当前会话环境】你当前正在与{master_part}沟通，"
        f"工作会话为{working_session_name or '未知'} id {working_session_id}"
    )
