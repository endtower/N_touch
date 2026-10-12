# -*- coding: utf-8 -*-
"""
adapt.web.context_api
=====================

LLM 上下文查看接口（事件链存储模型）.

源数据（全部收敛在 memory/event_chains/）::

    memory/event_chains/
      event_chain_20260912_223001_ab12/   ← 每个任务链一个文件夹
        conversation.json / pfca.json / proactive.json / social_chat.json
        quest_223045.json                 ← quest agent 唤醒分段（QuestAgent 落盘）
      unlinked/                           ← 链外写入 + legacy 迁移
        conversation.json                 ← 链外常驻
        pfca_223001.json                  ← 链外归档（时间戳名）
        conversation_archived_*.json      ← 旧格式迁移保留原名

端点:
  GET /context/chains
      -> {"chains": [{chain_id, is_active, latest_mtime, files:[{kind,name,size,mtime}]}],
          "unlinked": {"files": [...]}, "fetched_at": ...}
      链列表按 进行中优先 + 最近活动时间 倒序；未关联文件按 mtime 倒序。
  GET /context/file?chain=<chain_id|unlinked>&name=xxx
      -> {"kind": ..., "name": ..., "chain_id": ..., "entries": [...], "raw_size": ...}

kind 从文件名解析（{prefix}.json / {prefix}_{HHMMSS}.json 唤醒分段名 / legacy 前缀匹配），
解析不出为 null，前端按原始文件名展示。

查询只读取已落盘文件，不实例化 HistoryManager 或触发旧存档迁移。
只读, 不写; 不会触碰任何文件.
"""

from __future__ import annotations

import time
from typing import Any, Dict

from aiohttp import web

from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


from console_context import list_chains, read_chain_file


def _list_chains() -> Dict[str, Any]:
    from utils.history_manager import get_active_chain_ids
    return list_chains(set(get_active_chain_ids()))


def _read_chain_file(chain: str, name: str) -> Dict[str, Any]:
    return read_chain_file(chain, name)


# ============================================================
# aiohttp handlers
# ============================================================

async def handle_chains(request: web.Request) -> web.Response:
    try:
        data = _list_chains()
        data["fetched_at"] = time.time()
        return web.json_response(data)
    except Exception as e:
        logger.exception("[/context/chains] 失败")
        return web.json_response({"error": f"列出事件链失败: {e}"}, status=500)


async def handle_read_file(request: web.Request) -> web.Response:
    try:
        chain = request.query.get("chain", "").strip()
        name = request.query.get("name", "").strip()
        if not chain or not name:
            return web.json_response({"error": "chain 和 name 必填"}, status=400)
        try:
            data = _read_chain_file(chain, name)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        except FileNotFoundError as e:
            return web.json_response({"error": str(e)}, status=404)
        except PermissionError as e:
            return web.json_response({"error": str(e)}, status=403)
        return web.json_response(data)
    except Exception as e:
        logger.exception("[/context/file] 失败")
        return web.json_response({"error": f"读取文件失败: {e}"}, status=500)
