"""上下文文件的纯读取器；不导入业务模块，不触发迁移。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from paths import data_root

def _chains_root() -> Path:
    return data_root() / "memory" / "event_chains"

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

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]+\.json$")

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

def _file_meta(f: Path) -> Dict[str, Any]:
    st = f.stat()
    return {
        "name": f.name,
        "kind": _parse_kind(f.name),
        "size": st.st_size,
        "mtime": st.st_mtime,
    }

def list_chains(active_ids: set[str] | None = None) -> Dict[str, Any]:
    """扫描 event_chains/：链文件夹列表 + unlinked 文件列表。"""
    active_ids = active_ids or set()

    root = _chains_root()
    chains: List[Dict[str, Any]] = []
    unlinked_files: List[Dict[str, Any]] = []
    if root.exists():
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

def read_chain_file(chain: str, name: str) -> Dict[str, Any]:
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
