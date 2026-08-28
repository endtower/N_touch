#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
崩溃判定 Agent

【临时状态】本文件暂未被调用。
当前 emotional_interface.get_amy_description() 命中 should_trigger_crash 后
直接调 trigger_immediate_crash()，使用预设的 DEFAULT_CRASH_DESCRIPTION，
跳过崩溃 agent 的 LLM 调用。

恢复使用时：
1. emotional_interface.get_amy_description() 改回调 run_crash_agent_sync(snapshot)
2. 根据 agent decision 调 trigger_crash / trigger_not_crashed
"""

import asyncio
import json
import re
from typing import Optional, Dict, Any

from utils.logger import get_logger
from utils.llm_client_manager import call_llm
from brain.amy.constants import MoodSnapshot

logger = get_logger('amy_crash_agent', 'INFO')


_CRASH_SYSTEM_PROMPT = """你是崩溃判定 Agent，负责在情绪已经接近极限时，决定是否真的进入崩溃。

【背景】
- 你是 Amy 情绪系统的子模块
- 上一级的情绪判定已经在"按概率"的方式发出预警
- 你的任务是基于当前情绪状态和最近的想法，做出**人类式的最后一根稻草判断**
- 崩溃不是失败，而是情绪走到极限时的自然反应

【你的输入】
- 当前去甲肾上腺素值（ne）
- 当前多巴胺值（da）
- 最近 3 条想法（top_thoughts）

【你的输出】严格 JSON 格式：
{
  "decision": "crash" 或 "not_crash",
  "description": "崩溃期/安抚期的覆盖描述，第二人称，30字以内",
  "last_straw_desc": "如果崩，描述触发崩溃的'最后一根稻草'；如果不崩可为空字符串"
}

【判定原则】
- 如果 ne/da 都接近极限，且 top_thoughts 都是负向 → 倾向 crash
- 如果 ne/da 接近极限但 top_thoughts 中有正向 → 倾向 not_crash（再忍一下）
- 如果 ne/da 还远未到极限 → 强制 not_crash（即使概率命中也只是预警，不真崩）
- description 要"像人崩溃时会想的话"，不要堆叠修饰语
- 严格只输出 JSON，不要任何其他文字

示例输出 1（崩溃）：
{"decision":"crash","description":"你受够了。","last_straw_desc":"对方又重复了第三次"}

示例输出 2（再忍一下）：
{"decision":"not_crash","description":"你深吸一口气，还在努力控制自己。","last_straw_desc":""}
"""


def _build_user_message(snapshot: MoodSnapshot) -> str:
    """构造 user 消息：当前情绪 + 最近想法"""
    parts = [
        f"【当前情绪】",
        f"去甲肾上腺素: {snapshot.norepinephrine:.1f}",
        f"多巴胺: {snapshot.dopamine:.1f}",
        f"",
        f"【最近 3 条想法】",
    ]
    if not snapshot.top_thoughts:
        parts.append("（无活跃想法）")
    else:
        for i, t in enumerate(snapshot.top_thoughts[:3], 1):
            parts.append(f"{i}. [{t.act}] {t.desc}")
    return "\n".join(parts)


def _parse_agent_output(content: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中提取 JSON 块。
    兼容三种情况：
    1. 纯 JSON
    2. JSON 块嵌在 ```json ... ``` 中
    3. JSON 块嵌在 ``` ... ``` 中
    """
    content = content.strip()

    # 尝试 1：markdown code fence
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # 尝试 2：直接是 JSON
    if content.startswith("{"):
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

    # 尝试 3：找第一个 { 到最后一个 } 的内容
    brace_start = content.find("{")
    brace_end = content.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        candidate = content[brace_start:brace_end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    logger.warning(f"崩溃 agent 输出无法解析为 JSON: {content[:200]}")
    return None


async def run_crash_agent(snapshot: MoodSnapshot) -> Optional[Dict[str, Any]]:
    """异步调用崩溃 agent。返回 dict 或 None（失败时）。"""
    user_message = _build_user_message(snapshot)

    messages = [
        {"role": "system", "content": _CRASH_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    try:
        response = await call_llm(
            mode='basic-reason',
            messages=messages,
        )
        content = response.choices[0].message.content or ""
        logger.debug(f"崩溃 agent 原始输出: {content[:500]}")

        result = _parse_agent_output(content)
        if not result:
            return None

        # 字段校验
        decision = result.get("decision", "").strip()
        if decision not in ("crash", "not_crash"):
            logger.warning(f"崩溃 agent decision 字段非法: {decision}")
            return None

        description = result.get("description", "").strip()
        if not description:
            description = "你情绪失控了。" if decision == "crash" else "你深吸一口气，还在努力控制。"

        last_straw = result.get("last_straw_desc", "").strip()

        return {
            "decision": decision,
            "description": description,
            "last_straw_desc": last_straw,
        }

    except Exception as e:
        logger.error(f"崩溃 agent 调用异常: {e}", exc_info=True)
        return None


def run_crash_agent_sync(snapshot: MoodSnapshot) -> Optional[Dict[str, Any]]:
    """同步阻塞入口：emotional_interface.get_amy_description 调用。
    在已运行的事件循环里通过 run_until_complete 是不安全的，
    这里用 nest-asyncio 模式或直接调底层 asyncio.run 取决于调用方环境。

    设计选择：让 emotional_interface.get_amy_description 改成 async 后再 await，
    本函数保留同步入口是为了"PFCA 在这里被卡住"的语义显式化。
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 当前已在 async 上下文，不能用 asyncio.run
            # 这里用 ensure_future + 阻塞等待
            # 但这违反了"崩溃前停顿"的语义之外的设计简洁性
            # 实际项目中这里应该让 emotional_interface.get_amy_description 是 async
            # 然后 PFCA 在 await ei.get_amy_description() 时被卡住
            logger.warning(
                "run_crash_agent_sync 在 async 上下文中被调用，"
                "建议改用 await run_crash_agent(snapshot)"
            )
            # 退化为同步调底层 client（不阻塞 event loop）
            return _run_crash_agent_blocking(snapshot)
        else:
            return asyncio.run(run_crash_agent(snapshot))
    except RuntimeError:
        # 没有 event loop，直接 asyncio.run
        return asyncio.run(run_crash_agent(snapshot))


def _run_crash_agent_blocking(snapshot: MoodSnapshot) -> Optional[Dict[str, Any]]:
    """在已有 event loop 的线程中直接调用 call_llm（不走 asyncio.run）。
    注意：这会临时接管 event loop，仅用于崩溃 agent 的同步入口。
    """
    import concurrent.futures

    def _runner():
        return asyncio.run(run_crash_agent(snapshot))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_runner)
        return future.result(timeout=30)  # 崩溃 agent 最长等 30s