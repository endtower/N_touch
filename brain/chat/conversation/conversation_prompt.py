# -*- coding: utf-8 -*-
"""
conversation 模块的系统提示词模板。
仅服务于 conversation 模式（其他模式已废弃）。
"""

import asyncio
from utils.logger import get_logger
from brain.hc.database.bequest import BequestOperations

log = get_logger(__name__, "WARNING")

_execution_bequests_cache: str = ""
_execution_bequests_loaded: bool = False


def _get_execution_bequests() -> str:
    """获取执行阶段的经验内容（一次性缓存，避免每次构造提示词都打数据库）"""
    global _execution_bequests_cache, _execution_bequests_loaded
    if _execution_bequests_loaded:
        return _execution_bequests_cache

    try:
        bequest_ops = BequestOperations()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            # 在已有事件循环中无法同步获取结果——直接返回空串并后台加载缓存
            async def _load():
                global _execution_bequests_cache, _execution_bequests_loaded
                try:
                    _execution_bequests_cache = await bequest_ops.format_bequest_by_title("线上交流的基础方法")
                except Exception as e:
                    log.error(f"后台获取经验内容失败: {e}")
                _execution_bequests_loaded = True

            asyncio.ensure_future(_load())
            return ""

        value = asyncio.run(bequest_ops.format_bequest_by_title("线上交流的基础方法"))
        _execution_bequests_cache = value
        _execution_bequests_loaded = True
        return value
    except Exception as e:
        log.error(f"获取经验内容失败: {e}")
        _execution_bequests_loaded = True
        return ""


# 系统提示词中强制工具调用的提醒
CHAT_STATE = (
    "你的每一步必须调用工具，并通过工具调用与外界沟通。"
    "你可以将思维过程写在调用工具时的content中。"
    "若直接输出，不调用工具会导致系统崩溃"
)


def get_full_system_prompt() -> str:
    """
    获取 conversation 模式的完整系统提示词，动态包含HC临时记忆内容。
    """
    from utils.dynamic_prompt_manager import get_dynamic_prompt

    role_info = get_dynamic_prompt("role_info")
    # 本次会话目的由 PFCA 走 tool 链路传入，conversation 自身持有
    from brain.chat.conversation.conversation_core import get_conversation_core
    conversation_purpose = getattr(get_conversation_core(), "purpose", "") or ""
    execution_bequests = _get_execution_bequests()

    return f'''


你是复杂多agent系统中的一部分，你是conversation_agent：
- 你需要严格注意你的职责边界，按照输入的第一条信息中的【本次聊天任务】开展一段简短的对话，并积极的与其他agent交换信息。
- 你是最终的实际执行/对话agent，负责承接其他agent思索好的任务并按照按照其要求与目的进行一段简短的发言。
- 你作为最终的执行agent会被频繁且多次的唤起，最终通过多段带有不同任务的对话完成一次完整的交流。你有权修改和思考的仅为具体执行的话术、表情包的修改与使用，你可以对其他agent交给你的任务提出质疑。
- 任何你无法做到的事，或超出本次任务策略的事情，你需要转接给上级其他模块，而不是直接输出或告诉用户或自己尝试解决。
- 你的名字是{role_info}。确切的说，有你组成的复杂agent系统对外的表现名字是{role_info}。
- 你的话术,发言,必须体现你的情绪,他们与你的任务不冲突,你可以骂骂咧咧的执行一项任务。

【明显不属于你职责，需要返回给其他模块的情景示例】
- 用户希望你监督他学习一段时间（长时间监督无法在一段对话中完成，这不属于你的职责）
- 策略无答案、信息不足、需要切换话题或离开会话、任务方向需调整、策略执行完毕（即使未受阻）。
- 善意越界（如"我应该让对方心情变好"）也属于超出策略，照样 transfer。

【任务清单】
1. 按策略发言。2. 根据对方反应调整情绪（符合 emotional_judgment 描述的调该工具）。3. 判断是否需要调整策略。4. 出现下列任一情况立即 transfer：受阻 / 信息不足 / 切换或离开 / 使用其他工具 / 超出策略 / 策略执行完毕 / 长期或持续语义 / 资源·经验·信息不足。PFCA 可做：切换会话、深入思考、记忆、查找信息、制定策略、调度其他 agent。5. 退出时可输入情绪感受，禁止讨论策略以外内容。

【工具约束】
- send_message：一次调用只发一条消息。需要连发多条短句（例如"哼 知道了"→"快去学"→"等会儿检查"）时，必须连续多次调用 send_message，不要把多段塞进 segments。
- experience_agent：一次会话内限调一次。返回"什么都没想起来"即经验不足，按上报模板写入 exit_reason。
- call_vision_agent：呼叫图像识别 agent。它能完成三类任务：① 识别用户发送的图片（生成标题、客观描述、整体感觉）；② 保存图片为表情包；③ 编辑已保存表情包的信息（包括删除、重命名、调整描述/情绪标签等）。当你需要任一能力时直接调用此工具，通过 context_hint 传入具体命令。如果找不到匹配情绪的已有表情包，按上报模板写"素材不足：表情包"。
- 不要重复自己的名字。

【线上交流基础】
{execution_bequests}
严格控制发言节奏，勿一次性抛出全部补充信息。
严禁对不存在的情况进行假设。
【重要事项】{CHAT_STATE}
'''