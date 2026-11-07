# -*- coding: utf-8 -*-
"""通用经验辅助方法（bequest helpers）

存放 conversation / social_chat 共用的话术经验（bequest）检索与激活核心逻辑。
conversation 的 wait_and_observe（experience_scenario）与 social_chat 的 experience_agent 工具、两模块 input 经验激活共用这里的"向量检索"原语；
激活缓存的写入与文案拼装留在各模块——缓存来源不同：
conversation_bequest 单例 / social_chat_input_manager._activated_bequests。

经验库支持三类经验（speech 话术 / style 风格样本 / decision 决策，2026-11-04 起）；
本模块服务话术与风格样本，检索按 category 定向（决策经验不混入激活）。
话术是可直接说出的固定搭配，风格是他人原话样本，两者分开检索、分开注入。
"""

from typing import Any, Callable, Dict, List, Optional

from brain.hc.database.bequest import BequestOperations


async def search_speech_bequests(
    scenario: str,
    skip: Optional[Callable[[str], bool]] = None,
    top_k: int = 5,
    category: str = "speech",
) -> List[Dict[str, Any]]:
    """向量检索经验库（conversation wait_and_observe / social_chat experience_agent 共用核心）。

    category 默认 'speech'（话术固定搭配）；传 'style' 检索他人原话样本。

    过滤规则：
    - skip(bequest_id) 命中即跳过（已激活判定，由调用方提供）；
    - title / summary 任一为空跳过。

    调用方拿到结果后自行：写入激活缓存 + 拼装返回文案 +
    决定是否附带情感副作用（当前各调用方均不附带遭遇登记）。

    Args:
        scenario: 检索 query。
        skip: 已激活判定函数，入参 bequest_id，返回 True 表示跳过。
        top_k: 向量检索条数。
        category: 'speech'（默认，固定搭配）/ 'style'（他人原话样本）。

    Returns:
        [{"id": str, "title": str, "summary": str, "score": float}, ...]，按检索相关性排序。
    """
    bequest_ops = BequestOperations()
    scored = await bequest_ops.search_bequest_by_vector(
        scenario, top_k=top_k, category=category, record_usage=True,
    )

    matched: List[Dict[str, Any]] = []
    for bequest_id, score in scored:
        if skip is not None and skip(bequest_id):
            continue
        data = await bequest_ops.get_bequest_by_id(bequest_id)
        if not data:
            continue
        title = data.get("title", "")
        summary = data.get("summary", "")
        if title and summary:
            matched.append({"id": bequest_id, "title": title, "summary": summary, "score": score})
    return matched


async def activate_bequests_for_input(
    query: str,
    skip: Optional[Callable[[str], bool]] = None,
    top_k: int = 3,
    max_activate: int = 3,
    category: str = "speech",
) -> List[Dict[str, Any]]:
    """按输入文本做经验向量匹配，返回可激活的经验条目（input 经验激活共用核心）。

    category 默认 'speech'（话术固定搭配）；传 'style' 检索他人原话样本。

    过滤规则：
    - skip(bequest_id) 命中即跳过（已激活判定，由调用方提供）；
    - summary 为空跳过（title 为空记 "未知"）。

    调用方拿到结果后自行：写入激活缓存 + 拼装文案（两模块文案格式不同：
    conversation 按 header 分组追加，social_chat 逐条内联追加）。

    Args:
        query: 检索 query（通常为用户输入文本）。
        skip: 已激活判定函数，入参 bequest_id，返回 True 表示跳过。
        top_k: 向量检索条数。
        max_activate: 最多返回条数。
        category: 'speech'（默认，固定搭配）/ 'style'（他人原话样本）。

    Returns:
        [{"id": str, "title": str, "summary": str, "score": float}, ...]，最多 max_activate 条，按检索相关性排序。
    """
    if not query:
        return []

    bequest_ops = BequestOperations()
    scored = await bequest_ops.search_bequest_by_vector(
        query, top_k=top_k, category=category, record_usage=True,
    )

    matched: List[Dict[str, Any]] = []
    for bequest_id, score in scored:
        if skip is not None and skip(bequest_id):
            continue
        data = await bequest_ops.get_bequest_by_id(bequest_id)
        if not data:
            continue
        summary = data.get("summary", "")
        if not summary:
            continue
        matched.append({
            "id": bequest_id,
            "title": data.get("title", "未知"),
            "summary": summary,
            "score": score,
        })
        if len(matched) >= max_activate:
            break
    return matched
