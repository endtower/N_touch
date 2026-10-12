#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
消息仓库模块 (Message Repository)

此模块是系统的底层数据访问层，负责消息的持久化存储、检索和基础文件操作。
会话标识统一使用 session_id，格式为 "type_id"，例如 "private_1234567890" / "group_198431299"。

存储引擎：SQLite（aiosqlite，全异步）。
- 相比旧版 JSON 文件存储，SQLite 更可靠、支持并发、性能更好。
- 每条消息带自增序号 seq（游标语义的基础）。
- 游标机制（每消费者一个进度）统一管理已读进度，read 字段已废弃并移除。

主要类与方法：

MessageRepository类：
- store_message: 将消息存储到会话
- read_session_messages: 读取指定会话的消息列表(分页)
- get_session_status: 获取会话的状态信息
- delete_session: 删除指定的会话及所有消息
- list_sessions: 列出所有会话
- get_latest_session_messages: 获取目标会话的最后n条信息
- get_cursor / advance_cursor / get_unread_for: 游标机制（新增）

模块级别函数：
- get_message_store: 获取MessageRepository的单例实例
"""


from paths import data_root
import json
import logging
import os
import sys
import time
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional

import aiosqlite

# 导入工具模块
from utils.logger import setup_logging
from utils.error_handling import async_handle_errors, ValidationError, StorageError, ConfigurationError

# 设置日志
logger = setup_logging(__name__)

# 游标拉取单次上限（保底）：防止未读积压时一次拉取过多消息
MAX_UNREAD_FETCH_LIMIT = 50


class MessageRepository:
    """消息仓库类，负责消息的持久化存储、检索和基础文件操作

    会话标识统一使用 session_id（格式 "type_id"），存储采用 SQLite。
    单例由模块级 get_message_store 持有。
    """

    def __init__(self, storage_path: str = None, db_name: str = "chat_history.db"):
        """
        初始化MessageRepository实例

        Args:
            storage_path: 消息存储路径，如果为None则使用默认路径
            db_name: SQLite 数据库文件名（默认 chat_history.db，测试域可传 test_chat_history.db）
        """
        # 设置默认存储路径
        if storage_path is None:
            self.storage_path = data_root() / "memory" / "chat_history"
        else:
            self.storage_path = Path(storage_path)

        # 确保存储目录存在
        os.makedirs(self.storage_path, exist_ok=True)

        # SQLite 数据库文件路径（与 chat_history 同目录）
        self._db_path = self.storage_path / db_name
        self._conn: Optional[aiosqlite.Connection] = None
        # —— 初始化竞态修复（GPT 审查-1）——
        # 旧实现把连接先赋给 _conn 再分多个 await 建表，并发调用者会拿到
        # "已连接未建表"的半成品连接（空库首启必现 no such table）。
        # _ready 只有在连接+建表全部完成后才为 True；所有调用者经
        # _ensure_connection 的初始化锁等待，不允许绕过。
        self._ready = False
        self._closed = False
        self._init_lock = asyncio.Lock()

    # ============================================================
    # 连接管理
    # ============================================================

    async def _ensure_connection(self) -> aiosqlite.Connection:
        """确保数据库连接已建立（懒连接，初始化串行化）。

        初始化（连接 + PRAGMA + 建表）在初始化锁内用局部连接完成，成功后才
        发布到 self._conn 并置 _ready；失败或被取消（CancelledError 属
        BaseException，普通 except Exception 捕获不到）都关闭局部连接并回滚
        状态，绝不把半初始化连接留给并发调用者。
        """
        if self._ready and self._conn is not None:
            return self._conn
        async with self._init_lock:
            if self._ready and self._conn is not None:
                return self._conn
            if self._closed:
                raise StorageError("消息存储已关闭，不能再建立连接")
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
                cur = await conn.execute("PRAGMA journal_mode=WAL")
                await cur.fetchall()
                # 并发写等待：多进程同读写 chat_history.db 时短暂等待而非报锁
                # （PRAGMA 光标同样必须消费，见 database/core.py 的注释）
                cur = await conn.execute("PRAGMA busy_timeout=5000")
                await cur.fetchall()
                await self._create_tables(conn)
                if os.getenv("ABYSS_DEBUG_DB"):
                    probe = await conn.execute("SELECT name FROM sqlite_master")
                    names = [r[0] for r in await probe.fetchall()]
                    print(f"[dbdebug] pid={os.getpid()} db={self._db_path} "
                          f"tables={names}", file=sys.stderr, flush=True)
            except BaseException:
                if conn is not None:
                    try:
                        await conn.close()
                    except Exception:
                        pass
                raise
            self._conn = conn   # 全部就绪才发布
            self._ready = True
        return self._conn

    async def _create_tables(self, conn: aiosqlite.Connection) -> None:
        """建表（显式接受连接：初始化期间不依赖提前发布的 self._conn）。"""
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                session_name TEXT DEFAULT '',
                remark TEXT DEFAULT '',
                last_update_time REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                message_id TEXT NOT NULL,
                user_id TEXT DEFAULT '',
                user_name TEXT DEFAULT '',
                timestamp REAL DEFAULT 0,
                content_text TEXT DEFAULT '',
                content TEXT DEFAULT '[]',
                UNIQUE(session_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);

            CREATE TABLE IF NOT EXISTS cursors (
                session_id TEXT NOT NULL,
                consumer_id TEXT NOT NULL,
                position INTEGER DEFAULT -1,
                PRIMARY KEY (session_id, consumer_id)
            );
            """
        )
        await conn.commit()

    async def close(self) -> None:
        """关闭数据库连接（与初始化串行化，避免和建表竞争）。"""
        async with self._init_lock:
            self._closed = True
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
            self._ready = False

    async def ensure_ready(self) -> None:
        """显式就绪钩子（SystemCore start 用）：完成连接 + 建表，先于业务并发。"""
        await self._ensure_connection()

    async def reconfigure(self, storage_path: str | None = None,
                          db_name: str = "chat_history.db") -> None:
        """原地重指向存储位置（测试切库 / 恢复真实库）。

        在初始化锁内完成：关旧连接、换路径、复位就绪状态；持有同一实例的
        模块自动跟随。任何失败直接抛异常阻断，不 try-ex 兜底继续。
        """
        async with self._init_lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None
            self._ready = False
            self._closed = False
            if storage_path is None:
                self.storage_path = data_root() / "memory" / "chat_history"
            else:
                self.storage_path = Path(storage_path)
            os.makedirs(self.storage_path, exist_ok=True)
            self._db_path = self.storage_path / db_name

    # ============================================================
    # 内部辅助
    # ============================================================

    def _validate_session_params(self, session_id: str) -> None:
        """验证会话参数的有效性

        Args:
            session_id: 会话标识，格式为 "type_id"

        Raises:
            ValidationError: 当session_id无效时
        """
        if not session_id:
            raise ValidationError("无效的会话标识符: session_id为空")

    def _get_session_file_path(self, session_id: str) -> Path:
        """获取会话文件路径（兼容旧接口，返回 JSON 文件路径）。

        注意：新实现已改用 SQLite，此方法仅用于兼容旧调用方（如 message_advanced
        曾直接调用）。新代码不应依赖此方法。
        """
        self._validate_session_params(session_id)
        return self.storage_path / f"{session_id}.json"

    @staticmethod
    def _row_to_message(row: aiosqlite.Row) -> Dict[str, Any]:
        """把 SQLite 行转换为消息字典（保持旧 JSON 结构）。"""
        return {
            "message_id": row["message_id"],
            "user_id": row["user_id"],
            "user_name": row["user_name"],
            "timestamp": row["timestamp"],
            "content_text": row["content_text"],
            "content": json.loads(row["content"]) if row["content"] else [],
            "_seq": row["seq"],
        }

    async def _get_session_data(self, session_id: str) -> Dict[str, Any]:
        """获取会话的完整数据（兼容旧接口，返回 {session_info, message_list}）。"""
        conn = await self._ensure_connection()
        self._validate_session_params(session_id)

        # 会话信息
        cur = await conn.execute(
            "SELECT session_id, session_name, remark, last_update_time FROM sessions WHERE session_id=?",
            (session_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return {
                "session_info": {"session_id": session_id, "session_name": "", "remark": "", "last_update_time": 0},
                "message_list": [],
            }
        session_info = {
            "session_id": row["session_id"],
            "session_name": row["session_name"],
            "remark": row["remark"],
            "last_update_time": row["last_update_time"],
        }

        # 消息列表
        cur = await conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY seq ASC",
            (session_id,),
        )
        rows = await cur.fetchall()
        message_list = [self._row_to_message(r) for r in rows]

        return {"session_info": session_info, "message_list": message_list}

    # ============================================================
    # 公开方法（保留旧接口签名）
    # ============================================================

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def store_message(self, session_data: Dict[str, Any]) -> Dict[str, Any]:
        """将消息存储到会话中。

        Args:
            session_data: 包含会话信息和消息列表的数据字典：
            {
              "session_info": {"session_id", "session_name", "remark", "last_update_time"},
              "message_list": [...]
            }

        Returns:
            包含操作结果的字典
        """
        if "session_info" not in session_data:
            raise ValidationError("无效的会话数据: 缺少session_info字段")

        session_info = session_data["session_info"]
        if "session_id" not in session_info or not session_info["session_id"]:
            raise ValidationError("无效的会话数据: session_info中缺少有效的session_id字段")

        session_id = session_info["session_id"]

        if "message_list" not in session_data:
            raise ValidationError("无效的会话数据: 缺少message_list字段")

        message_list = session_data["message_list"]
        if not isinstance(message_list, list):
            raise ValidationError("无效的消息数据: message_list应为列表类型")

        if not message_list:
            raise ValidationError("无效的消息数据: message_list不能为空")

        conn = await self._ensure_connection()

        # 更新会话信息
        latest_timestamp = max(msg.get("timestamp", 0) for msg in message_list)

        session_update = {
            "session_id": session_id,
            "last_update_time": latest_timestamp,
        }
        if "session_name" in session_info:
            new_session_name = session_info["session_name"]
            if isinstance(new_session_name, str) and new_session_name.strip() \
                    and not new_session_name.startswith("群_"):
                session_update["session_name"] = new_session_name.strip()
        if "remark" in session_info:
            session_update["remark"] = session_info["remark"]

        try:
            await conn.execute(
                """INSERT INTO sessions (session_id, session_name, remark, last_update_time)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       session_name=excluded.session_name,
                       remark=excluded.remark,
                       last_update_time=excluded.last_update_time""",
                (session_id, session_update.get("session_name", ""),
                 session_update.get("remark", ""), latest_timestamp),
            )
            inserted, skipped = await self._insert_messages(conn, session_id, message_list)
            await conn.commit()
        except aiosqlite.Error as e:
            # 写入失败绝不吞掉：包装为 StorageError，由装饰器 raise_on 崩溃阻断
            raise StorageError(f"SQLite 写入失败 (session={session_id}): {e}") from e

        logger.debug(f"消息已成功存储到会话 {session_id}（新增 {inserted} 条，重投递跳过 {skipped} 条）")
        first_message_id = message_list[0].get("message_id", "unknown")

        return {
            "status": "success",
            "session_id": session_id,
            "message_id": first_message_id,
            "stored_at": str(self._db_path),
            "total_messages_stored": len(message_list),
        }

    async def _insert_messages(
        self, conn: aiosqlite.Connection, session_id: str,
        message_list: List[Dict[str, Any]],
    ) -> tuple[int, int]:
        """逐条插入消息，返回 (新增数, 重投递跳过数)。

        去重语义（UNIQUE(session_id, message_id) 冲突时的两种情形）：

        - 同一事件重投递（message_id + timestamp + content_text 完全一致）
          → 幂等跳过（如 NapCat 重连重推），记 DEBUG 日志；
        - NapCat message_id 复用与历史消息冲突（时间戳/内容不同）
          → 换合成 ID（"{message_id}_{timestamp}"）落库，记 WARNING 日志。

        背景（KNOWN_ISSUES #11）：NapCat 的 message_id 是有限位数的整数，
        会话历史上 ID 复用相当频繁（2026-09-06 复盘事故中一个晚上同会话
        撞了 8 条），INSERT OR IGNORE 会把新消息静默丢弃且不留任何日志。
        """
        inserted = 0
        skipped = 0
        for msg in message_list:
            message_id = msg.get("message_id", "")
            if not message_id:
                continue

            cur = await conn.execute(
                """INSERT OR IGNORE INTO messages
                   (session_id, message_id, user_id, user_name, timestamp, content_text, content)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    message_id,
                    msg.get("user_id", ""),
                    msg.get("user_name", ""),
                    msg.get("timestamp", 0),
                    msg.get("content_text", ""),
                    json.dumps(msg.get("content", []), ensure_ascii=False),
                ),
            )
            if cur.rowcount:
                inserted += cur.rowcount
                continue

            # (session_id, message_id) 冲突：区分"重投递"与"ID 复用"
            dup = await conn.execute(
                """SELECT timestamp, content_text FROM messages
                   WHERE session_id=? AND message_id=?""",
                (session_id, message_id),
            )
            row = await dup.fetchone()
            if row is not None \
                    and int(row["timestamp"] or 0) == int(msg.get("timestamp", 0) or 0) \
                    and (row["content_text"] or "") == (msg.get("content_text") or ""):
                # 同一事件重投递 → 幂等跳过
                skipped += 1
                logger.debug(
                    f"重复事件重投递，幂等跳过 (session={session_id}, msg={message_id})"
                )
                continue

            # ID 复用冲突 → 合成 ID 落库，绝不静默丢弃
            synthetic_id = f"{message_id}_{int(msg.get('timestamp', 0) or 0)}"
            cur = await conn.execute(
                """INSERT OR IGNORE INTO messages
                   (session_id, message_id, user_id, user_name, timestamp, content_text, content)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    synthetic_id,
                    msg.get("user_id", ""),
                    msg.get("user_name", ""),
                    msg.get("timestamp", 0),
                    msg.get("content_text", ""),
                    json.dumps(msg.get("content", []), ensure_ascii=False),
                ),
            )
            if cur.rowcount:
                inserted += cur.rowcount
                logger.warning(
                    f"message_id 与历史消息冲突，已换合成 ID 落库: "
                    f"{message_id} -> {synthetic_id} (session={session_id})"
                )
            else:
                # 合成 ID 也已存在 = 同一冲突消息的重推 → 幂等跳过
                skipped += 1
                logger.debug(
                    f"冲突消息重投递，幂等跳过 (session={session_id}, msg={synthetic_id})"
                )
        return inserted, skipped

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """获取会话的状态信息（会话元数据 + 消息总数）。

        注意：read 字段已废弃（游标制），不再返回 unread_count / read_count。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        # 会话信息
        cur = await conn.execute(
            "SELECT session_id, session_name, remark, last_update_time FROM sessions WHERE session_id=?",
            (session_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return {
                "session_info": {"session_id": session_id, "session_name": "", "remark": "", "last_update_time": 0},
                "total_count": 0,
            }
        session_info = {
            "session_id": row["session_id"],
            "session_name": row["session_name"],
            "remark": row["remark"],
            "last_update_time": row["last_update_time"],
        }

        # 消息总数
        cur = await conn.execute(
            "SELECT COUNT(*) AS total FROM messages WHERE session_id=?",
            (session_id,),
        )
        stat = await cur.fetchone()
        total = stat["total"] or 0

        return {
            "session_info": session_info,
            "total_count": total,
        }

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def read_session_messages(self, session_id: str,
                                    page: int = 1, page_size: int = 10) -> Dict[str, Any]:
        """读取指定会话的消息列表（分页）。"""
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 10

        # 会话信息
        cur = await conn.execute(
            "SELECT session_id, session_name, remark, last_update_time FROM sessions WHERE session_id=?",
            (session_id,),
        )
        row = await cur.fetchone()
        session_info = {
            "session_id": session_id,
            "session_name": row["session_name"] if row else "",
            "remark": row["remark"] if row else "",
            "last_update_time": row["last_update_time"] if row else 0,
        }

        # 总数
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=?", (session_id,)
        )
        total_count = (await cur.fetchone())["c"]

        # 分页
        offset = (page - 1) * page_size
        cur = await conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY seq ASC LIMIT ? OFFSET ?",
            (session_id, page_size, offset),
        )
        rows = await cur.fetchall()
        message_list = [self._row_to_message(r) for r in rows]

        total_pages = (total_count + page_size - 1) // page_size if total_count else 0

        return {
            "status": "success",
            "session_info": session_info,
            "message_list": message_list,
            "total_count": total_count,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def delete_session(self, session_id: str) -> Dict[str, Any]:
        """删除指定的会话及所有消息。"""
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        await conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        await conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
        await conn.execute("DELETE FROM cursors WHERE session_id=?", (session_id,))
        await conn.commit()

        logger.info(f"会话 {session_id} 已删除")
        return {"status": "success", "session_id": session_id, "deleted": True}

    @async_handle_errors(error_types=(StorageError,), log_level=logging.ERROR)
    async def list_sessions(self) -> List[Dict[str, Any]]:
        """列出所有会话。"""
        conn = await self._ensure_connection()

        cur = await conn.execute(
            """SELECT s.session_id, s.session_name, s.remark, s.last_update_time,
                      (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.session_id) AS message_count
               FROM sessions s ORDER BY s.last_update_time DESC"""
        )
        rows = await cur.fetchall()

        sessions = []
        for row in rows:
            sessions.append({
                "session_id": row["session_id"],
                "session_name": row["session_name"],
                "remark": row["remark"],
                "message_count": row["message_count"],
                "last_update_time": row["last_update_time"],
            })
        return sessions

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_speech_pressure(self, session_id: str) -> Dict[str, Any]:
        """60秒实际输出字符数，以及最后一次输出后是否有用户消息（按seq）。"""
        conn = await self._ensure_connection()
        self._validate_session_params(session_id)
        async with conn.execute(
            "SELECT user_id, content_text FROM messages WHERE session_id=? AND timestamp>?",
            (session_id, time.time() - 60),
        ) as cursor:
            rows = await cursor.fetchall()
        characters = sum(
            sum(not ch.isspace() for ch in (row["content_text"] or ""))
            for row in rows if row["user_id"] == "system"
        )
        async with conn.execute(
            "SELECT user_id FROM messages WHERE session_id=? ORDER BY seq DESC LIMIT 1",
            (session_id,),
        ) as cursor:
            last = await cursor.fetchone()
        return {"characters": characters, "user_replied": bool(last and last["user_id"] != "system")}

    async def get_latest_session_messages(self, session_id: str, n: int = 10) -> Dict[str, Any]:
        """获取目标会话的最后n条信息，按时间顺序排列。

        Args:
            session_id: 会话标识。
            n: 最多返回条数。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        # 会话信息
        cur = await conn.execute(
            "SELECT session_id, session_name, remark, last_update_time FROM sessions WHERE session_id=?",
            (session_id,),
        )
        row = await cur.fetchone()
        session_info = {
            "session_id": session_id,
            "session_name": row["session_name"] if row else "",
            "remark": row["remark"] if row else "",
            "last_update_time": row["last_update_time"] if row else 0,
        }

        # 总数
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=?", (session_id,)
        )
        total_count = (await cur.fetchone())["c"]

        # 最后 n 条（按 seq 倒序取 n 条，再正序返回）
        cur = await conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY seq DESC LIMIT ?",
            (session_id, n),
        )
        rows = await cur.fetchall()
        message_list = [self._row_to_message(r) for r in rows]
        message_list.reverse()

        return {
            "status": "success",
            "session_info": session_info,
            "message_list": message_list,
            "total_count": total_count,
        }

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def query_session_messages(self, session_id: str,
                                     limit: int = 20,
                                     speaker: Optional[str] = None,
                                     start_ts: Optional[float] = None,
                                     end_ts: Optional[float] = None) -> Dict[str, Any]:
        """按发言人 / 时间范围组合查询会话消息，返回最新的 limit 条命中。

        Args:
            session_id: 会话标识。
            limit: 最多返回条数（取最新的 limit 条命中）。
            speaker: 发言人筛选（user_name LIKE 模糊匹配，通配符自动转义）；None 不限。
            start_ts: 起始时间（含，epoch 秒）；None 不限。
            end_ts: 结束时间（含，epoch 秒）；None 不限。

        Returns:
            {
              "messages": 命中消息列表（按 seq 升序），
              "total_hits": 满足条件的命中总数（含未显示的更早命中），
              "total_count": 会话消息总数,
            }
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        cond = "session_id=?"
        params: list = [session_id]
        if speaker:
            pattern = f"%{speaker.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
            cond += " AND user_name LIKE ? ESCAPE '\\'"
            params.append(pattern)
        if start_ts is not None:
            cond += " AND timestamp >= ?"
            params.append(float(start_ts))
        if end_ts is not None:
            cond += " AND timestamp <= ?"
            params.append(float(end_ts))

        cur = await conn.execute(f"SELECT COUNT(*) AS c FROM messages WHERE {cond}", params)
        total_hits = (await cur.fetchone())["c"]

        cur = await conn.execute(
            f"SELECT * FROM messages WHERE {cond} ORDER BY seq DESC LIMIT ?",
            (*params, limit),
        )
        rows = await cur.fetchall()
        messages = [self._row_to_message(r) for r in rows]
        messages.reverse()

        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=?", (session_id,)
        )
        total_count = (await cur.fetchone())["c"]

        return {
            "messages": messages,
            "total_hits": total_hits,
            "total_count": total_count,
        }

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def session_exists(self, session_id: str) -> bool:
        """判断会话是否存在（sessions 表有记录）。"""
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()
        cur = await conn.execute(
            "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
        )
        return await cur.fetchone() is not None

    # ============================================================
    # 游标机制（新增）
    # ============================================================

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_cursor(self, session_id: str, consumer_id: str) -> int:
        """获取某消费者在指定会话的游标位置（已读到的最大 seq）。

        Args:
            session_id: 会话标识。
            consumer_id: 消费者标识（如 "conversation" / "social_chat"）。

        Returns:
            游标位置（-1 表示从未读过）。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()
        cur = await conn.execute(
            "SELECT position FROM cursors WHERE session_id=? AND consumer_id=?",
            (session_id, consumer_id),
        )
        row = await cur.fetchone()
        return row["position"] if row else -1

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def advance_cursor(self, session_id: str, consumer_id: str, position: int) -> None:
        """推进某消费者在指定会话的游标位置。

        Args:
            session_id: 会话标识。
            consumer_id: 消费者标识。
            position: 新的游标位置（已读到的最大 seq）。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()
        await conn.execute(
            """INSERT INTO cursors (session_id, consumer_id, position) VALUES (?, ?, ?)
               ON CONFLICT(session_id, consumer_id) DO UPDATE SET position=excluded.position""",
            (session_id, consumer_id, position),
        )
        await conn.commit()

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_unread_for(self, session_id: str, consumer_id: str,
                             limit: Optional[int] = None,
                             newest: bool = False) -> List[Dict[str, Any]]:
        """获取某消费者未读的新消息（seq > 游标位置）。

        这是游标机制的核心：每个消费者只拿自己没读过的新消息，互不顶掉、不重复。

        Args:
            session_id: 会话标识。
            consumer_id: 消费者标识。
            limit: 最多返回条数（None 表示使用默认上限 MAX_UNREAD_FETCH_LIMIT）。
            newest: 是否取最新的 limit 条（seq 最大的），默认 False 取最旧的 limit 条。
                返回列表始终按 seq 升序。

        Returns:
            未读消息列表（按 seq 升序），每条带瞬态标记 `_unread=True`
            （供格式化层渲染 [新] 前缀，不落库）。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        position = await self.get_cursor(session_id, consumer_id)

        # 保底：单次拉取不超过 MAX_UNREAD_FETCH_LIMIT 条，防止未读积压时一次拉取过多
        effective_limit = min(limit, MAX_UNREAD_FETCH_LIMIT) if limit is not None else MAX_UNREAD_FETCH_LIMIT

        order = "DESC" if newest else "ASC"
        cur = await conn.execute(
            f"SELECT * FROM messages WHERE session_id=? AND seq > ? ORDER BY seq {order} LIMIT ?",
            (session_id, position, effective_limit),
        )
        rows = await cur.fetchall()
        messages = [self._row_to_message(r) for r in rows]
        if newest:
            messages.reverse()
        # 按游标判定为未读的消息，标注 _unread 供 [新] 前缀使用
        for m in messages:
            m["_unread"] = True
        return messages

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def count_unread_for(self, session_id: str, consumer_id: str) -> int:
        """统计某消费者未读消息总数（seq > 游标位置）。

        供封装层判断未读是否溢出（用于"共 xxxx 条未读，仅显示最新 N 条"提示）。

        Args:
            session_id: 会话标识。
            consumer_id: 消费者标识。

        Returns:
            未读消息总数。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        position = await self.get_cursor(session_id, consumer_id)
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=? AND seq > ?",
            (session_id, position),
        )
        row = await cur.fetchone()
        return row["c"]

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def count_unread_user_for(self, session_id: str, consumer_id: str) -> int:
        """统计某消费者未读的【用户消息】总数（seq > 游标位置 且 user_id != 'system'）。

        truck 层用它检测"新收到的用户消息"，排除助手自身回复（system 消息），
        避免 conversation 回复落库后 truck 误判为新的未读而重复触发。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        position = await self.get_cursor(session_id, consumer_id)
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=? AND seq > ? AND user_id != 'system'",
            (session_id, position),
        )
        row = await cur.fetchone()
        return row["c"]

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_messages_before_cursor(self, session_id: str, consumer_id: str,
                                         limit: int) -> List[Dict[str, Any]]:
        """拉取游标位置之前（seq <= 游标）的最新 limit 条消息。

        供封装层在未读不足时补足历史（已读）消息。返回的补充消息不标注
        `_unread`，格式化层据此不渲染 [新] 前缀。

        Args:
            session_id: 会话标识。
            consumer_id: 消费者标识。
            limit: 最多返回条数（必须 > 0）。

        Returns:
            补充的历史消息列表（按 seq 升序）。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()

        if limit <= 0:
            return []

        position = await self.get_cursor(session_id, consumer_id)
        cur = await conn.execute(
            "SELECT * FROM messages WHERE session_id=? AND seq <= ? ORDER BY seq DESC LIMIT ?",
            (session_id, position, limit),
        )
        rows = await cur.fetchall()
        messages = [self._row_to_message(r) for r in rows]
        messages.reverse()
        return messages

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_messages_by_seq_range(self, session_id: str, start_seq: int,
                                        end_seq: int) -> List[Dict[str, Any]]:
        """按 seq 闭区间 [start_seq, end_seq] 拉取消息（含两端）。

        供会话范围游标使用：conversation 记录本次会话拉取的消息 seq 范围，
        结束时用此方法把该范围内的消息一次性取回。

        Args:
            session_id: 会话标识。
            start_seq: 起始 seq（含）。
            end_seq: 结束 seq（含）。

        Returns:
            消息列表（按 seq 升序）。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()
        cur = await conn.execute(
            "SELECT * FROM messages WHERE session_id=? AND seq >= ? AND seq <= ? ORDER BY seq ASC",
            (session_id, start_seq, end_seq),
        )
        rows = await cur.fetchall()
        return [self._row_to_message(r) for r in rows]

    # ============================================================
    # Web 层支持（供 adapt.web.db_store 复用同一数据源）
    # ============================================================

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def delete_message(self, session_id: str, message_id: str) -> bool:
        """删除指定会话中的单条消息。返回是否找到并删除。"""
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()
        cur = await conn.execute(
            "DELETE FROM messages WHERE session_id=? AND message_id=?",
            (session_id, message_id),
        )
        await conn.commit()
        return cur.rowcount > 0

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def delete_messages(self, session_id: str, message_ids: List[str]) -> List[str]:
        """批量删除指定会话中的消息（单事务）。返回实际删除的 message_id 列表。"""
        self._validate_session_params(session_id)
        if not message_ids:
            return []
        conn = await self._ensure_connection()
        placeholders = ",".join("?" * len(message_ids))
        # 先查存在的 id（rowcount 不告知具体删了哪些, 广播需要确切列表）
        cur = await conn.execute(
            f"SELECT message_id FROM messages WHERE session_id=? AND message_id IN ({placeholders})",
            (session_id, *message_ids),
        )
        rows = await cur.fetchall()
        existing = [r["message_id"] for r in rows]
        if not existing:
            return []
        ph = ",".join("?" * len(existing))
        await conn.execute(
            f"DELETE FROM messages WHERE session_id=? AND message_id IN ({ph})",
            (session_id, *existing),
        )
        await conn.commit()
        return existing

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def create_session(self, session_id: str, session_name: str = "",
                             remark: str = "") -> Dict[str, Any]:
        """创建空会话（只写 sessions 行，不写消息）。已存在则更新名称。"""
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()
        await conn.execute(
            """INSERT INTO sessions (session_id, session_name, remark, last_update_time)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                   session_name=excluded.session_name,
                   remark=excluded.remark""",
            (session_id, session_name, remark, int(time.time())),
        )
        await conn.commit()
        return {"status": "success", "session_id": session_id}

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_latest_messages(self, session_id: str, limit: int) -> Dict[str, Any]:
        """取会话末尾 limit 条消息（按 seq 升序返回）。"""
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()
        cur = await conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY seq DESC LIMIT ?",
            (session_id, limit),
        )
        rows = await cur.fetchall()
        messages = [self._row_to_message(r) for r in rows]
        messages.reverse()
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=?", (session_id,)
        )
        total = (await cur.fetchone())["c"]
        return {
            "messages": messages,
            "has_more": total > limit,
            "oldest_message_id": messages[0]["message_id"] if messages else None,
            "total": total,
        }

    @async_handle_errors(error_types=(StorageError, ValidationError), log_level=logging.ERROR)
    async def get_messages_before(self, session_id: str, before_message_id: str,
                                  limit: int) -> Dict[str, Any]:
        """取 before_message_id 之前（不含）的 limit 条消息，按 seq 升序。

        found=False 表示 before_message_id 不存在（可能已被删），调用方回退到末尾。
        """
        self._validate_session_params(session_id)
        conn = await self._ensure_connection()
        cur = await conn.execute(
            "SELECT seq FROM messages WHERE session_id=? AND message_id=?",
            (session_id, before_message_id),
        )
        row = await cur.fetchone()
        if row is None:
            return {"messages": [], "has_more": False,
                    "oldest_message_id": None, "found": False, "total": 0}
        cur = await conn.execute(
            "SELECT * FROM messages WHERE session_id=? AND seq < ? ORDER BY seq DESC LIMIT ?",
            (session_id, row["seq"], limit),
        )
        rows = await cur.fetchall()
        messages = [self._row_to_message(r) for r in rows]
        messages.reverse()
        cur = await conn.execute(
            "SELECT 1 FROM messages WHERE session_id=? AND seq < ? LIMIT 1",
            (session_id, row["seq"]),
        )
        has_more = await cur.fetchone() is not None
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id=?", (session_id,)
        )
        total = (await cur.fetchone())["c"]
        return {
            "messages": messages,
            "has_more": has_more,
            "oldest_message_id": messages[0]["message_id"] if messages else None,
            "found": True,
            "total": total,
        }


# ============================================================
# 全局单例
# ============================================================

_message_store_instance: Optional[MessageRepository] = None


def get_message_store(storage_path: str = None, db_name: str = "chat_history.db") -> MessageRepository:
    """获取全局消息存储实例（单例）。

    Args:
        storage_path: 消息存储路径，如果为None则使用默认路径（仅首次创建时生效）
        db_name: SQLite 数据库文件名（默认 chat_history.db，测试域可传 test_chat_history.db）

    单例已存在时再传不同参数直接报错（切换库走 switch_message_store /
    实例 reconfigure，禁止静默换目标）。
    """
    global _message_store_instance
    if _message_store_instance is None:
        _message_store_instance = MessageRepository(storage_path, db_name)
    elif storage_path is not None or db_name != "chat_history.db":
        raise ConfigurationError(
            "MessageRepository 单例已创建，拒绝以不同参数重复初始化；"
            "切换库请用 switch_message_store()"
        )
    return _message_store_instance


async def switch_message_store(storage_path: str | None = None,
                               db_name: str = "chat_history.db") -> MessageRepository:
    """原地重指向全局消息存储单例（测试模式切库 / 恢复真实库）。

    关闭当前连接 → 把**同一实例**的存储路径指向新位置。
    因为改的是持有者手上的同一实例（含 MessageHandler / MessageAdvancedManager /
    GlobalSessionHistory / adapt.web.db_store 构造时捕获的引用），
    所有持有者自动跟随，无需重置任何依赖单例。

    任何失败（如旧连接无法关闭）直接抛异常阻断，不 try-ex 兜底继续。
    """
    return await get_message_store().reconfigure(storage_path, db_name)
