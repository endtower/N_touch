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

    @property
    def db_path(self) -> Path:
        return self._db_path

    async def _ensure_connection(self) -> aiosqlite.Connection:
        """确保数据库连接已建立（懒连接）。"""
        if self._conn is None:
            try:
                self._conn = await aiosqlite.connect(self._db_path)
                self._conn.row_factory = aiosqlite.Row
                # PRAGMA 光标必须消费完毕（fetchall 复位语句）：
                # 悬空的 active 语句会让同连接后续 DDL（如 DROP TABLE）报
                # SQLITE_LOCKED "database table is locked"
                cur = await self._conn.execute("PRAGMA journal_mode=WAL")
                await cur.fetchall()
                await self._create_tables()
            except Exception as e:
                # 失败时关闭并清空连接，防止半初始化连接残留
                # （aiosqlite 工作线程会挂住进程退出，且下次调用会误跳过建表）
                if self._conn is not None:
                    try:
                        await self._conn.close()
                    except Exception:
                        pass
                    self._conn = None
                raise StorageError(f"SQLite 连接失败: {str(e)}")
        return self._conn

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
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                summary TEXT DEFAULT '',
                next_occurrence TEXT DEFAULT '',
                summary_vector BLOB,
                is_vectorized INTEGER DEFAULT 0,
                start_time TEXT DEFAULT '',
                end_time TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

            -- 经验表（独立，话术专用：category 固定 'speech'）
            CREATE TABLE IF NOT EXISTS bequests (
                bequest_id TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                summary TEXT DEFAULT '',
                category TEXT DEFAULT 'speech',
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

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def reconfigure(self, db_path: str | None = None) -> None:
        """原地重指向数据库文件（测试切库 / 恢复真实库）。

        先关闭当前连接再换路径；持有同一实例的模块自动跟随，无需重置依赖单例。
        任何失败（如旧连接无法关闭）直接抛异常阻断，不 try-ex 兜底继续。
        """
        await self.close()
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
