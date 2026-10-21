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

from paths import data_root
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
            self._db_path = data_root() / "memory" / "memory.db"
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

    async def conversation_allowed(self, session_id: str) -> bool:
        """该会话所属社交实体是否已在记忆里勾选「允许 conversation」。

        角色表先于群组表匹配；会话 id 自带 private_/group_ 前缀，两者不会重名。
        """
        if not session_id:
            return False
        conn = await self._ensure_connection()
        for table in ("characters", "groups"):
            cur = await conn.execute(f"SELECT contacts, allow_conversation FROM {table}")
            for row in await cur.fetchall():
                contacts = json.loads(row["contacts"] or "[]")
                if any(c.get("session_id") == session_id for c in contacts):
                    return bool(row["allow_conversation"])
        return False

    async def get_conversation_allowed_session_ids(self) -> set:
        """一次性返回所有「允许 conversation」实体的 session_id 集合。

        角色表与群组表都扫，只取 allow_conversation=1 的实体，收集其 contacts 里的
        每个 session_id。供通讯录一次性标注放行会话，避免逐会话调用
        conversation_allowed() 引发的 O(n²) 全表扫。
        """
        conn = await self._ensure_connection()
        allowed: set = set()
        for table in ("characters", "groups"):
            cur = await conn.execute(f"SELECT contacts, allow_conversation FROM {table}")
            for row in await cur.fetchall():
                if not row["allow_conversation"]:
                    continue
                for c in json.loads(row["contacts"] or "[]"):
                    sid = c.get("session_id")
                    if sid:
                        allowed.add(sid)
        return allowed

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
                allow_conversation INTEGER DEFAULT 0,
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
                allow_conversation INTEGER DEFAULT 0,
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
            -- 2026-10-11 收敛：title（标题）+ context（要传递给下次启动时的上下文）
            -- + next_occurrence（下次唤醒时间）。status 仅 active / expired。
            -- 触发即过期（task_truck 交付 PFCA 后自动置 expired），续办靠新建。
            -- 2026-10-19 新增 kind：onetime（一次性，到期即过期）/ ongoing（持续任务，
            -- 无时间点、永不到期，由 PRE 规则层巡检）。
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                context TEXT DEFAULT '',
                kind TEXT DEFAULT 'onetime',
                status TEXT DEFAULT 'active',
                next_occurrence TEXT DEFAULT '',
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
                retrieval_count INTEGER NOT NULL DEFAULT 0,
                last_maintained_at REAL,
                manual_maintenance_count INTEGER NOT NULL DEFAULT 0,
                title_vector BLOB,
                is_vectorized INTEGER DEFAULT 0,
                start_time TEXT DEFAULT '',
                end_time TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_bequests_category ON bequests(category);

            -- 人工维护的决策锦囊，与自动话术经验分开。
            CREATE TABLE IF NOT EXISTS decision_kits (
                kit_id TEXT PRIMARY KEY,
                advice TEXT NOT NULL,
                dilemma TEXT NOT NULL,
                category TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            -- 见闻正文向量缓存；原始见闻仍保存在独立台账。
            CREATE TABLE IF NOT EXISTS observation_vectors (
                note_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                model_key TEXT NOT NULL,
                vector BLOB NOT NULL
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS critical_actions (
                action_id TEXT PRIMARY KEY, action TEXT NOT NULL, axis TEXT NOT NULL,
                title TEXT NOT NULL, details_json TEXT NOT NULL, task_ids_json TEXT NOT NULL DEFAULT '[]',
                issues_json TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL,
                reset_applied INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                error TEXT NOT NULL DEFAULT '', archived_at REAL
            );
            CREATE TABLE IF NOT EXISTS failure_reason_cache (
                reason_id TEXT PRIMARY KEY, source TEXT NOT NULL, session_id TEXT NOT NULL,
                title TEXT NOT NULL, act TEXT NOT NULL, failure_reason TEXT NOT NULL,
                created_at REAL NOT NULL
            );

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
        await self._migrate_conversation_flag()
        await self._migrate_tasks_kind()
        await self._migrate_critical_actions_archive()

    async def _migrate_critical_actions_archive(self) -> None:
        """维护申请增加归档时间；不改 state，避免 recover_resets 漏挑待重置记录。"""
        assert self._conn is not None
        cur = await self._conn.execute("PRAGMA table_info(critical_actions)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "archived_at" not in cols:
            await self._conn.execute(
                "ALTER TABLE critical_actions ADD COLUMN archived_at REAL"
            )
            await self._conn.commit()

    async def _migrate_tasks_kind(self) -> None:
        """任务区分一次性 / 持续任务（老数据一律按 onetime 处理）。"""
        assert self._conn is not None
        cur = await self._conn.execute("PRAGMA table_info(tasks)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "kind" not in cols:
            await self._conn.execute(
                "ALTER TABLE tasks ADD COLUMN kind TEXT DEFAULT 'onetime'"
            )
            await self._conn.commit()

    async def _migrate_conversation_flag(self) -> None:
        """社交实体增加 conversation 放行开关（角色/群组各一列，默认关闭）。"""
        assert self._conn is not None
        for table in ("characters", "groups"):
            cur = await self._conn.execute(f"PRAGMA table_info({table})")
            cols = {r["name"] for r in await cur.fetchall()}
            if "allow_conversation" not in cols:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN allow_conversation INTEGER DEFAULT 0"
                )
        await self._conn.commit()

    async def _migrate_bequests_table(self) -> None:
        """经验统计独立于情感系统；旧维护记录只用于一次性迁入人工统计。"""
        assert self._conn is not None
        cur = await self._conn.execute("PRAGMA table_info(bequests)")
        cols = {r["name"] for r in await cur.fetchall()}
        for name, definition in {
            "persona": "TEXT DEFAULT ''", "retrieval_count": "INTEGER NOT NULL DEFAULT 0",
            "last_maintained_at": "REAL", "manual_maintenance_count": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in cols:
                await self._conn.execute(f"ALTER TABLE bequests ADD COLUMN {name} {definition}")
        done = await (await self._conn.execute("SELECT 1 FROM schema_migrations WHERE name='experience_statistics_v1'")).fetchone()
        if not done:
            tables = {r['name'] for r in await (await self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
            if "memory_usage_events" in tables:
                await self._conn.execute("""UPDATE bequests SET retrieval_count=(SELECT count(*) FROM memory_usage_events
                    WHERE kind='experience' AND item_id=bequests.bequest_id AND event_type IN ('vector_returned','returned'))""")
            if "maintenance_reviews" in tables:
                await self._conn.execute("""UPDATE bequests SET
                    manual_maintenance_count=(SELECT count(*) FROM maintenance_reviews WHERE kind='experience' AND item_id=bequests.bequest_id),
                    last_maintained_at=(SELECT max(reviewed_at) FROM maintenance_reviews WHERE kind='experience' AND item_id=bequests.bequest_id)""")
            # 旧申请与重置台账封存，运行路径不再读取或补发其中的效果。
            for name in ("maintenance_reviews", "memory_usage_events", "maintenance_requests", "growth_actions"):
                if name in tables and "retired_" + name not in tables:
                    await self._conn.execute(f"ALTER TABLE {name} RENAME TO retired_{name}")
            await self._conn.execute("INSERT INTO schema_migrations VALUES ('experience_statistics_v1')")
        await self._conn.commit()

    async def _migrate_tasks_table(self) -> None:
        """任务表结构收敛（2026-10-11：重建为 title/context/next_occurrence）。

        `CREATE TABLE IF NOT EXISTS` 不会改动已存在的旧表。旧库结构
        （brief/conclusion/stage/notes/deadline）整体重建为空表；旧记录按用户
        确认一并丢弃，不做迁移（任务语义已从「可更新」收敛为「触发即过期、续办靠新建」）。

        幂等：新表已含 context 列且无 brief 列时直接返回（零开销）。
        迁移失败直接上抛崩溃阻断，不吞错。
        """
        assert self._conn is not None
        cur = await self._conn.execute("PRAGMA table_info(tasks)")
        cols = {r["name"] for r in await cur.fetchall()}
        if "context" in cols and "brief" not in cols:
            return

        logger.info("tasks 表结构迁移：重建为 title/context/next_occurrence（旧记录丢弃）")
        await self._conn.execute("DROP TABLE IF EXISTS tasks")
        await self._conn.execute(
            """
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                context TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                next_occurrence TEXT DEFAULT '',
                start_time TEXT DEFAULT '',
                end_time TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            )
            """
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)"
        )
        await self._conn.commit()
        logger.info("tasks 表结构迁移完成（旧记录已清空）")

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
                self._db_path = data_root() / "memory" / "memory.db"
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
