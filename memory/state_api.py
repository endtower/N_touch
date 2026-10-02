# -*- coding: utf-8 -*-
"""
Memory 模块状态契约（stable public API）

供预设测试环境使用：把 memory.db / chat_history.db 的整体状态导出/还原为可读 JSON。
- 载体：JSON（可肉眼编辑、可版本化），见 docs/archive/preset_test_env_design.md
- 归属：本契约归 Memory 模块暴露；tests 只做隔离运行时的组装与应用，不复刻表结构。
- 全异步；任何获取失败直接抛异常阻断，不用 try-ex 兜底继续。

用法（测试场景）：
    async with isolated_storage(scope) as scope:      # tests/core 提供的隔离运行时
        state = await MemoryState.capture()           # 捕获现场（可选）
        await MemoryState.from_json_file(SCENARIO).restore(scope)   # 还原预置
"""

import json
from pathlib import Path
from typing import Dict, Any, List

import aiosqlite

from brain.hc.database.core import get_memory_db
from memory.message_repository import get_message_store

# 允许为 NULL / 动态列做通用 dump 时 safety：列名直接来自结果集，仅选取非 sqlite 系统表
_SYSTEM_TABLE_PREFIX = "sqlite_"


def _rows_to_json(rows) -> List[Dict[str, Any]]:
    """aiosqlite 行 → dict 列表（BLOB 字节归一化为可 JSON 序列化的值）。"""
    return [{k: _normalize_value(v) for k, v in dict(r).items()} for r in rows]


def _normalize_value(value: Any) -> Any:
    """把 SQLite 返回的 bytes 归一化为可 JSON 序列化的值。

    记忆库的 BLOB 均为 JSON 编码的向量（serialize_vector 产生），
    解码为 list 以保持可读可编辑；若解码失败（非 UTF-8/非 JSON）则回退 base64 字符串。
    """
    if isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            import base64
            return base64.b64encode(raw).decode("ascii")
    return value


async def _dump_sqlite(db_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """把 SQLite 库整体导出为 {表名: [行...]}（不触碰单例，独立连接只读）。"""
    connected = aiosqlite.connect(str(db_path))
    conn = await connected.__aenter__()
    try:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE ? ORDER BY name",
            (_SYSTEM_TABLE_PREFIX + "%",),
        )
        tables = [r["name"] for r in await cur.fetchall()]
        out: Dict[str, List[Dict[str, Any]]] = {}
        for table in tables:
            cur = await conn.execute(f"SELECT * FROM {table}")
            out[table] = _rows_to_json(await cur.fetchall())
        return out
    finally:
        await conn.close()


async def _restore_sqlite(provider, provider_arg: Path, tables: Dict[str, List[Dict[str, Any]]], **provider_kw) -> None:
    """把 {表名: [行...]} 写入隔离库。

    provider: get_memory_db / get_message_store。
    provider_arg: 目标 db 文件（memory）或存储目录（chat_history）。
    provider_kw: 透传给 provider 的额外参数（如 chat db 文件名）。
    会通过 provider 建立并保持指向隔离库的单例实例，使业务无参调用读到预置数据。
    """
    inst = provider(str(provider_arg), **provider_kw)
    conn = await inst._ensure_connection()  # 建表（schema 归模块持有）
    for table, rows in tables.items():
        if not rows:
            continue
        # 目标表实际存在的列：快照导出时全列 SELECT *，schema 随版本演进可能
        # 缺列（如旧版 read 字段已移除）或新列，仅插入交集列，容忍 schema 漂移。
        cur = await conn.execute(f"PRAGMA table_info({table})")
        schema = await cur.fetchall()
        existing = {r["name"] for r in schema}
        columns = [c for c in rows[0].keys() if c in existing]
        if not columns:
            continue
        collist = ",".join(columns)
        placeholders = ",".join("?" for _ in columns)
        values = [tuple(_restore_value(r.get(c)) for c in columns) for r in rows]
        await conn.executemany(
            f"INSERT OR REPLACE INTO {table} ({collist}) VALUES ({placeholders})",
            values,
        )
    await conn.commit()


def _restore_value(value: Any) -> Any:
    """把 JSON 快照中的值还原为 SQLite 可绑定值。

    - list：快照中向量 BLOB 已被解码为 JSON list，写回时重新编码为 bytes。
    - 其余：原样返回（str/int/float/None）。
    """
    if isinstance(value, list):
        from brain.hc.database.core import serialize_vector
        return serialize_vector(value)
    return value


class MemoryState:
    """Memory 模块状态契约（stable public API，JSON 载体）。

    属性：
        memory:       memory.db 各表导出（characters/groups/events/bequests/fuzzy_roles）
        chat_history: chat_history.db 各表导出（sessions/messages/cursors）
    """

    def __init__(
        self,
        memory: Dict[str, List[Dict[str, Any]]],
        chat_history: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        self.memory = memory
        self.chat_history = chat_history

    @classmethod
    async def capture(cls) -> "MemoryState":
        """从当前实际存储（单例指向的库）导出整体状态。"""
        memory = await _dump_sqlite(Path(get_memory_db().db_path))
        chat_history = await _dump_sqlite(Path(get_message_store()._db_path))
        return cls(memory=memory, chat_history=chat_history)

    @classmethod
    async def capture_memory(cls) -> "MemoryState":
        """仅从当前实际存储导出 memory.db（memory 分块镜像/预设）。"""
        return cls(memory=await _dump_sqlite(Path(get_memory_db().db_path)), chat_history={})

    @classmethod
    async def capture_chat(cls) -> "MemoryState":
        """仅从当前实际存储导出 chat_history.db（消息记录分块镜像/预设）。"""
        return cls(memory={}, chat_history=await _dump_sqlite(Path(get_message_store()._db_path)))

    @classmethod
    async def capture_from(cls, memory_path: Path, chat_path: Path) -> "MemoryState":
        """从**显式指定**的真实库文件导出整体状态（与当前单例指向无关）。

        供"正式聊天环境镜像"抓取使用：即使单例已重定向到测试库，
        仍能读取真实 memory.db / chat_history.db 的整体状态。
        某一路径为 None 或文件不存在 → 对应块为空 dict（不阻断）。
        """
        memory = {}
        if memory_path is not None and Path(memory_path).exists():
            memory = await _dump_sqlite(Path(memory_path))
        chat_history = {}
        if chat_path is not None and Path(chat_path).exists():
            chat_history = await _dump_sqlite(Path(chat_path))
        return cls(memory=memory, chat_history=chat_history)

    async def restore(self, scope_dir: Path, db_name: str = "memory.db", chat_db_name: str = "chat_history.db") -> None:
        """把 JSON 写回隔离目录的 memory.db / chat_history.db。

        scope_dir: StorageScope 的 root（目录）。
        db_name: 记忆库文件名（默认 memory.db，隔离域可传 test_memory.db）。
        chat_db_name: 消息库文件名（默认 chat_history.db，隔离域可传 test_chat_history.db）。
        本方法须在 isolated_storage 作用域内调用；单例指向由 provider 建立。
        """
        if self.memory:
            await self.restore_memory(scope_dir, db_name)
        if self.chat_history:
            await self.restore_chat(scope_dir, chat_db_name)

    async def restore_memory(self, scope_dir: Path, db_name: str = "memory.db") -> None:
        """仅还原 memory.db 分块（memory 预设/镜像）。"""
        if self.memory:
            await _restore_sqlite(
                get_memory_db, scope_dir / db_name, self.memory
            )

    async def restore_chat(self, scope_dir: Path, chat_db_name: str = "chat_history.db") -> None:
        """仅还原 chat_history.db 分块（消息记录预设/镜像）。"""
        if self.chat_history:
            await _restore_sqlite(
                get_message_store, scope_dir / "chat_history", self.chat_history, db_name=chat_db_name
            )

    # ---- 情景文档（JSON 文件）存/取 ----

    def to_json_file(self, path: Path) -> None:
        """导出为情景文档（可手动编辑）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "module": "memory",
            "state": {
                "memory": self.memory,
                "chat_history": self.chat_history,
            },
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    async def from_json_file(cls, path: Path) -> "MemoryState":
        """读入预设情景文档，复用 restore 复现。"""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("module") != "memory":
            raise ValueError(f"情景文件 {path.name} 不属于 memory 模块")
        state = data["state"]
        return cls(memory=state.get("memory", {}), chat_history=state.get("chat_history", {}))

    @classmethod
    async def from_memory_json_file(cls, path: Path) -> "MemoryState":
        """读入 memory 分块情景文档（仅 memory.db，供分块配置）。"""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("module") != "memory":
            raise ValueError(f"情景文件 {path.name} 不属于 memory 模块")
        return cls(memory=data["state"].get("memory", {}), chat_history={})

    @classmethod
    async def from_chat_json_file(cls, path: Path) -> "MemoryState":
        """读入 chat 分块情景文档（仅 chat_history.db，供分块配置）。"""
        path = Path(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("module") != "chat":
            raise ValueError(f"情景文件 {path.name} 不属于 chat 模块")
        return cls(memory={}, chat_history=data["state"].get("chat_history", {}))

    def to_memory_json_file(self, path: Path) -> None:
        """导出 memory 分块情景文档（仅 memory.db）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"module": "memory", "state": {"memory": self.memory}}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def to_chat_json_file(self, path: Path) -> None:
        """导出 chat 分块情景文档（仅 chat_history.db，消息记录镜像）。"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"module": "chat", "state": {"chat_history": self.chat_history}}
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )