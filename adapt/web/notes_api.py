# -*- coding: utf-8 -*-
"""
adapt.web.notes_api
===================

记事本（灵光一现 notepad）状态查看接口（只读）.

对前端「状态」选项卡暴露记事本当前状态:
  - pfca_manual       : PFCA 当前可见的记事本手册（render_manual(max_chars=1000)，
                        与 pfca_input_manager 注入 PFCA 上下文的同一出口，即
                        「PFCA 当前能看到哪些笔记」）
  - all               : 全部未归档（活动池）笔记文档（notes/ 顶层 *.json，
                        含标题/时间/起因/总结/逐条记录）
  - unprocessed_chars : 未处理笔记字符数（反思触发统计口径，
                        count_unprocessed_chars：活动池全部笔记，
                        不排除任何来源；反思笔记整篇硬性 <150 字）

端点:
  GET /notes -> {"pfca_manual": str, "all": [...], "unprocessed_chars": int,
                 "fetched_at": ...}

只读, 不写; 读取失败直接上抛崩溃阻断（按 user_rules，防止错误扩大蔓延）。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List

from aiohttp import web

from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


async def handle_get_notes(request: web.Request) -> web.Response:
    """GET /notes  返回记事本当前状态（只读）。"""
    from brain.hc.shortlight import get_shortlight_notepad

    notepad = get_shortlight_notepad()

    # 1. PFCA 视角手册：与 pfca_input_manager 注入 PFCA 上下文同一出口（max_chars=1000）。
    #    手册渲染失败直接上抛崩溃阻断（不吞错）。
    pfca_manual = await notepad.render_manual(max_chars=1000)

    # 2. 全部未归档（活动池）笔记：按落库时间倒序（最新在前）。
    docs: List[Dict[str, Any]] = await notepad.get_all()
    docs.sort(key=lambda d: (d.get("finalize_time") or ""), reverse=True)

    # 3. 未处理笔记字符数（反思触发统计口径）。
    unprocessed_chars = await notepad.count_unprocessed_chars()

    return web.json_response({
        "pfca_manual": pfca_manual,
        "all": docs,
        "unprocessed_chars": unprocessed_chars,
        "fetched_at": time.time(),
    })
