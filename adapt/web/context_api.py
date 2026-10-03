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

首次访问时 lazy 触发各 HistoryManager 实例化，一次性幂等迁移旧存档。
只读, 不写; 不会触碰任何文件.
"""

from __future__ import annotations
from paths import data_root

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from aiohttp import web

from utils.logger import get_logger

logger = get_logger(__name__, "DEBUG")


# HistoryManager 前缀 -> 前端 kind（与 utils/history_manager.py 的映射一致）
_PREFIX_TO_KIND = {
    "conversation": "chat",
    "observe": "chat",
    "temporary": "chat",
    "pfca": "pfca",
    "proactive": "proactive",
    "social_chat": "social_chat",
    "quest": "quest",
    "persona": "persona",
    # 人格粒度动态前缀（PersonaAgent.history_prefix = persona_{persona_name}）
    "persona_assistant": "persona_assistant",
    "persona_normal": "persona_normal",
    "persona_task": "persona_task",
    "vision": "vision",
    "bequest": "bequest",
    "memory": "memory",
    "light_agent": "light_agent",
}

# 安全: 文件名必须只允许字母数字 _ - (防 path traversal)
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.json$")

# 安全: 链 ID / 目录名只允许字母数字 _ -
_SAFE_CHAIN_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _parse_kind(name: str) -> Optional[str]:
    """从文件名解析 kind。

    - {prefix}.json（固定名文件）: 精确匹配
    - {prefix}_{HHMMSS}.json / {prefix}_legacy_*.json / {prefix}_archived_*.json:
      最长前缀匹配（social_chat 优先于 social 等嵌套前缀场景）
    - 解析不出: None（前端按原始文件名展示）
    """
    stem = name[:-len(".json")]
    if stem in _PREFIX_TO_KIND:
        return _PREFIX_TO_KIND[stem]
    best: Optional[tuple] = None
    for prefix, kind in _PREFIX_TO_KIND.items():
        if stem.startswith(prefix + "_") and (best is None or len(prefix) > len(best[0])):
            best = (prefix, kind)
    return best[1] if best else None


def _project_root() -> Path:
    """可写数据根（memory/event_chains 等运行数据都在这）."""
    return data_root()


def _chains_root() -> Path:
    return _project_root() / "memory" / "event_chains"


def ensure_legacy_migrated() -> None:
    """确保七个 HistoryManager 单例已实例化（触发一次性 legacy 迁移）。

    前端读 /context/chains 前调用；迁移在各 manager __init__ 中幂等执行
    （_migrated_keys 去重），重复调用零开销。
    """
    from brain.chat.chat_history_manager import get_history_manager
    from brain.pfc.pfca.pfca_history_manager import get_pfca_history_manager
    from brain.att.proactive.proactive_history_manager import get_proactive_history_manager
    from brain.base.light_agent.quest_history_manager import get_quest_history_manager
    from brain.base.light_agent.light_history_manager import get_light_history_manager

    get_history_manager("conversation")
    get_history_manager("observe")
    get_history_manager("temporary")
    get_history_manager("social_chat")
    get_pfca_history_manager()
    get_proactive_history_manager()
    get_quest_history_manager()
    # light agent 系：实例化以触发 legacy 迁移（persona 为人格粒度动态前缀）
    for prefix in ("persona", "persona_assistant", "persona_normal", "persona_task",
                   "vision", "bequest", "memory", "light_agent"):
        get_light_history_manager(prefix)


def _file_meta(f: Path) -> Dict[str, Any]:
    st = f.stat()
    return {
        "name": f.name,
        "kind": _parse_kind(f.name),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }


def _list_chains() -> Dict[str, Any]:
    """扫描 event_chains/：链文件夹列表 + unlinked 文件列表。"""
    from utils.history_manager import get_active_chain_ids

    root = _chains_root()
    chains: List[Dict[str, Any]] = []
    unlinked_files: List[Dict[str, Any]] = []
    if root.exists():
        active_ids = get_active_chain_ids()
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            if entry.name == "unlinked":
                for f in entry.iterdir():
                    if f.is_file() and f.name.endswith(".json"):
                        unlinked_files.append(_file_meta(f))
                continue
            files: List[Dict[str, Any]] = []
            latest = 0.0
            for f in entry.iterdir():
                if f.is_file() and f.name.endswith(".json"):
                    meta = _file_meta(f)
                    files.append(meta)
                    latest = max(latest, meta["mtime"])
            # 链内按写入时间正序（还原写入顺序）
            files.sort(key=lambda x: x["mtime"])
            chains.append({
                "chain_id": entry.name,
                "is_active": entry.name in active_ids,
                "latest_mtime": latest,
                "files": files,
            })
    # 进行中的链置顶, 其余按最近活动倒序; unlinked 按 mtime 倒序
    chains.sort(key=lambda c: (not c["is_active"], -c["latest_mtime"]))
    unlinked_files.sort(key=lambda x: -x["mtime"])
    return {"chains": chains, "unlinked": {"files": unlinked_files}}


def _read_chain_file(chain: str, name: str) -> Dict[str, Any]:
    """读取 event_chains/{chain}/{name}（unlinked 为链外区）。

    返回 entries 透传所有 JSON 可序列化字段（保留 tool_calls /
    reasoning_content / name / tool_call_id, 前端 LLM→Tool 配对卡片需要）。
    """
    if not _SAFE_NAME_RE.match(name):
        raise ValueError(f"name 不合法: {name!r}")
    if not _SAFE_CHAIN_RE.match(chain):
        raise ValueError(f"chain 不合法: {chain!r}")
    root = _chains_root().resolve()
    d = (root / chain).resolve()
    if d.parent != root:
        raise PermissionError(f"拒绝: {chain} 超出 event_chains")
    fp = d / name
    if not fp.exists() or not fp.is_file():
        raise FileNotFoundError(f"{fp} 不存在")
    # 防止符号链接外溢: resolve 后必须仍在 d 内
    fp_resolved = fp.resolve()
    try:
        fp_resolved.relative_to(d)
    except ValueError:
        raise PermissionError(f"拒绝: {name} 超出 {chain}")
    raw = fp.read_text(encoding="utf-8")
    size = len(raw.encode("utf-8"))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")
    # 把原始 entry 的所有字段都透传出来; 只保留 JSON 可序列化的字段,
    # None 保留 None, 复杂类型 json 强转保底, 不强行 str()
    entries: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            entry: Dict[str, Any] = {}
            for k, v in item.items():
                if isinstance(v, (str, int, float, bool)) or v is None or isinstance(v, list) or isinstance(v, dict):
                    entry[k] = v
                else:
                    try:
                        entry[k] = json.loads(json.dumps(v, ensure_ascii=False, default=str))
                    except Exception:
                        pass
            # role 缺省给空字符串
            entry.setdefault("role", "")
            entries.append(entry)
    elif isinstance(data, dict):
        # PFCA 极少出现 dict 形态, 也兜一下
        entry = {k: v for k, v in data.items()
                 if isinstance(v, (str, int, float, bool)) or v is None or isinstance(v, list) or isinstance(v, dict)}
        entry.setdefault("role", "")
        entries.append(entry)
    return {
        "kind": _parse_kind(name),
        "name": name,
        "chain_id": None if chain == "unlinked" else chain,
        "entries": entries,
        "raw_size": size,
    }


# ============================================================
# aiohttp handlers
# ============================================================

async def handle_chains(request: web.Request) -> web.Response:
    try:
        ensure_legacy_migrated()
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
