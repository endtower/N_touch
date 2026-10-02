# -*- coding: utf-8 -*-
"""
记忆数据库核心模块（SQLite）

以 00000000（LLM 扮演的角色，无名字）为中心的记忆存储。
所有信息从 00000000 指向其他对象（角色 / 群组 / 事件 / 经验 / 模糊角色）。

存储引擎：SQLite（aiosqlite，全异步，WAL）。
- 角色表 / 群组表：00000000 指向的其他角色 / 群组，含多值字段（别名、联系方式，JSON 数组）
  与单值字段（印象、熟悉度、群组成员印象）。
- 任务表 / 经验表 / 模糊角色表：独立。
- 向量以 BLOB 存 JSON 序列化，Python 侧算余弦相似度。
"""

import asyncio
import json
import os
from pathlib import Path
from typing import Optional

import aiosqlite

from utils.logger import get_logger
from utils.error_handling import StorageError, ConfigurationError

logger = get_logger(__name__, "INFO")


class MemoryDatabase:
    """记忆数据库核心类（全异步；单例由模块级 get_memory_db 持有）"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            # 默认存储在项目根目录的 memory 目录下，与 chat_history.db 同级
            project_root = Path(__file__).resolve().parent.parent.parent.parent
            self._db_path = project_root / "memory" / "memory.db"
        else:
            self._db_path = Path(db_path)
        os.makedirs(self._db_path.parent, exist_ok=True)
        self._conn: Optional[aiosqlite.Connection] = None
        # —— 初始化竞态修复（GPT 审查-1）——：_ready 前连接视为半成品，
        # 所有调用者经 _ensure_connection 的初始化锁等待，不允许绕过。
        self._ready = False
        self._closed = False
        self._init_lock = asyncio.Lock()

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def _ensure_connection(self) -> aiosqlite.Connection:
        """确保数据库连接已建立（懒连接，初始化串行化）。

        初始化（连接 + PRAGMA + 建表 + 迁移）在初始化锁内完成，全部成功才置
        _ready 放行；失败或被取消（CancelledError 属 BaseException）都关闭
        连接并回滚状态，绝不把"已连接未建表"的半成品连接留给并发调用者。
        """
        if self._ready and self._conn is not None:
            return self._conn
        async with self._init_lock:
            if self._ready and self._conn is not None:
                return self._conn
            if self._closed:
                raise StorageError("记忆数据库已关闭，不能再建立连接")
            # 清掉取消/失败窗口内可能残留的半初始化连接
            if self._conn is not None:
                try:
                    await self._conn.close()
                except Exception:
                    pass
                self._conn = None
            conn = None
            try:
                conn = await aiosqlite.connect(self._db_path)
                conn.row_factory = aiosqlite.Row
                self._conn = conn   # 供 _create_tables/_migrate_* 内部使用
                cur = await conn.execute("PRAGMA journal_mode=WAL")
                await cur.fetchall()
                cur = await conn.execute("PRAGMA busy_timeout=5000")
                await cur.fetchall()
                await self._create_tables()
                await self._migrate_tasks_table()
                self._ready = True
            except BaseException:
                self._conn = None
                self._ready = False
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                raise
        return self._conn

    async def ensure_ready(self) -> None:
        """显式就绪钩子（SystemCore start 用）：完成连接 + 建表 + 迁移，先于业务并发。"""
        await self._ensure_connection()

    async def _create_tables(self) -> None:
        """建表。"""
        assert self._conn is not None
        await self._conn.executescript(
            """
            -- 角色表（00000000 → 其他角色）
            CREATE TABLE IF NOT EXISTS characters (
                uuid TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                aliases TEXT DEFAULT '[]',
                impression TEXT DEFAULT '',
                familiarity TEXT DEFAULT '',
                contacts TEXT DEFAULT '[]',
                relationship TEXT DEFAULT '',
                affinity_events TEXT DEFAULT '[]',
                usage_count INTEGER DEFAULT 0,
                last_used_at TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                vector BLOB,
                is_vectorized INTEGER DEFAULT 0,
                vector_updated_at TEXT DEFAULT ''
            );

            -- 群组表（00000000 → 群组）
            CREATE TABLE IF NOT EXISTS groups (
                uuid TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                aliases TEXT DEFAULT '[]',
                impression TEXT DEFAULT '',
                familiarity TEXT DEFAULT '',
                member_impression TEXT DEFAULT '',
                contacts TEXT DEFAULT '[]',
                self_image TEXT DEFAULT '',
                usage_count INTEGER DEFAULT 0,
                last_used_at TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                vector BLOB,
                is_vectorized INTEGER DEFAULT 0,
                vector_updated_at TEXT DEFAULT ''
            );

            -- 旧事件表（已废弃，任务系统替代；启动时彻底删除旧数据）
            DROP TABLE IF EXISTS events;

            -- 任务表（独立）
            -- 2026-10-01 改造：summary 单列拆为四个独立语义列——
            -- brief（简要总结：前因与目的）/ conclusion（已获取的汇报结论）/
            -- stage（当前阶段：还需要做什么）/ notes（任务笔记）。
            -- 列独立承载语义，不再靠「首行当前阶段 + 平铺笔记」的文本约定拼接。
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                brief TEXT DEFAULT '',
                conclusion TEXT DEFAULT '',
                stage TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                next_occurrence TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                start_time TEXT DEFAULT '',
                end_time TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

            -- 经验表（独立，话术专用：category 固定 'speech'）
            -- 2026-10-01 新增 persona 列：人格绑定标签（''=全局通用）。
            -- 绑定某人格的经验仅在以该人格进行的会话中自动召回。
            CREATE TABLE IF NOT EXISTS bequests (
                bequest_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                category TEXT DEFAULT 'speech',
                persona TEXT DEFAULT '',
                title_vector BLOB,
                is_vectorized INTEGER DEFAULT 0,
                start_time TEXT DEFAULT '',
                end_time TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_bequests_category ON bequests(category);

            -- 模糊角色表（独立，唯一单独建表的依附性节点）
            CREATE TABLE IF NOT EXISTS fuzzy_roles (
                uuid TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                content TEXT DEFAULT '',
                source_session_id TEXT DEFAULT '',
                source_entity_type TEXT DEFAULT '',
                source_entity_uuid TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_fuzzy_roles_name ON fuzzy_roles(name);
            """
        )
        await self._conn.commit()
        await self._migrate_tasks_table()
        await self._migrate_bequests_table()

    async def _migrate_bequests_table(self) -> None:
        """经验表结构迁移（2026-10-01：新增 persona 人格绑定标签列）。

        `CREATE TABLE IF NOT EXISTS` 不会改动已存在的旧表，旧库补一列
        （ALTER TABLE ADD COLUMN，存量行默认 ''＝全局通用）。
        幂等：已含 persona 列时直接返回（零开销）。迁移失败直接上抛崩溃阻断。
        """
        assert self._conn is not None
        cur = await self._conn.execute("PRAGMA table_info(bequests)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "persona" in cols:
            return

        logger.info("bequests 表结构迁移：新增 persona 人格绑定标签列")
        await self._conn.execute(
            "ALTER TABLE bequests ADD COLUMN persona TEXT DEFAULT ''"
        )
        await self._conn.commit()

    async def _migrate_tasks_table(self) -> None:
        """任务表结构迁移（2026-10-01 二次收敛：summary 拆四列）。

        `CREATE TABLE IF NOT EXISTS` 不会改动已存在的旧表，故旧库走一次重建：
        重命名 → 建新表 → 逐行搬迁（旧的 summary 数据整体丢弃，四个新列一律置空，
        待 quest agent 下次推进时按新语义重新填写）→ 删旧表 → 重建索引。

        幂等：新表已含 brief 列时直接返回（零开销）。
        迁移失败直接上抛崩溃阻断，不吞错。
        """
        assert self._conn is not None
        cur = await self._conn.execute("PRAGMA table_info(tasks)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "brief" in cols:
            return

        logger.info("tasks 表结构迁移：summary 拆为 brief/conclusion/stage/notes 四列")
        cur = await self._conn.execute(
            "SELECT task_id, title, status, next_occurrence, deadline, "
            "start_time, end_time, updated_at FROM tasks"
        )
        rows = await cur.fetchall()

        await self._conn.execute("ALTER TABLE tasks RENAME TO tasks_old")
        await self._conn.execute(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                brief TEXT DEFAULT '',
                conclusion TEXT DEFAULT '',
                stage TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                next_occurrence TEXT DEFAULT '',
                deadline TEXT DEFAULT '',
                start_time TEXT DEFAULT '',
                end_time TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
            """
        )
        for r in rows:
            await self._conn.execute(
                "INSERT INTO tasks (task_id, title, status, brief, conclusion, "
                "stage, notes, next_occurrence, deadline, start_time, end_time, "
                "updated_at) VALUES (?, ?, ?, '', '', '', '', ?, ?, ?, ?, ?)",
                (
                    r["task_id"], r["title"], r["status"],
                    r["next_occurrence"] or "", r["deadline"] or "",
                    r["start_time"] or "", r["end_time"] or "",
                    r["updated_at"] or "",
                ),
            )
        await self._conn.execute("DROP TABLE tasks_old")
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
        )
        await self._conn.commit()
        logger.info(f"tasks 表结构迁移完成，搬迁 {len(rows)} 条（旧概况已丢弃）")

    async def close(self) -> None:
        """关闭数据库连接（与初始化串行化，避免和建表/迁移竞争）。"""
        async with self._init_lock:
            self._closed = True
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
            self._ready = False

    async def reconfigure(self, db_path: str | None = None) -> None:
        """原地重指向数据库文件（测试切库 / 恢复真实库）。

        在初始化锁内完成：关旧连接、换路径、复位就绪状态；持有同一实例的
        模块自动跟随。任何失败直接抛异常阻断，不 try-ex 兜底继续。
        """
        async with self._init_lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
            self._ready = False
            self._closed = False
            if db_path is None:
                # 恢复真实库：默认 memory/memory.db
                project_root = Path(__file__).resolve().parent.parent.parent.parent
                self._db_path = project_root / "memory" / "memory.db"
            else:
                self._db_path = Path(db_path)


# ============================================================
# 全局单例
# ============================================================

_memory_db_instance: Optional[MemoryDatabase] = None


def get_memory_db(db_path: str = None) -> MemoryDatabase:
    """获取记忆数据库单例实例。

    db_path 仅首次创建时生效；单例已存在时再传不同路径直接报错
    （切换库走 switch_memory_db / 实例 reconfigure，禁止静默换目标）。
    """
    global _memory_db_instance
    if _memory_db_instance is None:
        _memory_db_instance = MemoryDatabase(db_path)
    elif db_path is not None and Path(db_path) != _memory_db_instance.db_path:
        raise ConfigurationError(
            f"MemoryDatabase 单例已创建（{_memory_db_instance.db_path}），"
            f"拒绝以不同路径 {db_path!r} 重复初始化；切换库请用 switch_memory_db()"
        )
    return _memory_db_instance


async def switch_memory_db(db_path: str | None = None) -> MemoryDatabase:
    """原地重指向记忆数据库单例（测试模式切库 / 恢复真实库）。"""
    return await get_memory_db().reconfigure(db_path)


# ============================================================
# 向量工具（BLOB 序列化 / 反序列化 / 余弦相似度）
# ============================================================

def serialize_vector(vector) -> Optional[bytes]:
    """把向量（list[float]）序列化为 BLOB。"""
    if vector is None:
        return None
    return json.dumps(vector).encode("utf-8")


def deserialize_vector(blob) -> Optional[list]:
    """把 BLOB 反序列化为向量（list[float]）。"""
    if blob is None:
        return None
    try:
        return json.loads(blob.decode("utf-8"))
    except Exception:
        return None


def cosine_similarity(vec_a: list, vec_b: list) -> float:
    """计算两个向量的余弦相似度。"""
    import numpy as np
    a = np.array(vec_a)
    b = np.array(vec_b)
    if len(a) != len(b):
        return 0.0
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))
