# -*- coding: utf-8 -*-
"""
adapt.web.identity_store
========================

WebRuntime 的 JSON 持久化层。

文件: `<data_dir>/web_runtime.json`
- 单文件, 不分片
- 原子写 (先写 .tmp 再 rename), 防崩溃导致半截
- 启动时若不存在 → 调用 ensure_initialized() 自动初始化

数据 schema:
{
    "version": 1,
    "default_identity": {                    # 新建群聊会话时的种子（取自 RoleConfig.MASTER_ID / MASTER_NAME）
        "user_id": "<master_id>",
        "user_name": "<master_name>"
    },
    "sessions": {
        "<bare_session_id>": SessionRuntime(...),
        ...
    }
}
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from utils import setup_logging

from config.role_settings import role_config

from .models import Identity, SessionRuntime

logger = setup_logging("adapt.web.identity_store")


RUNTIME_FILE_NAME = "web_runtime.json"
RUNTIME_VERSION = 1

# 匹配统一 session_id（"private_xxx" / "group_xxx"）
_SESSION_ID_RE = re.compile(r"^(private|group)_(.+)$", re.IGNORECASE)


class IdentityStore:
    """管理 web_runtime.json 的读写 + 自动初始化。"""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.file = self.data_dir / RUNTIME_FILE_NAME
        self._lock = asyncio.Lock()
        # 内存缓存, 启动时 load 一次
        self._default_identity: Identity = Identity(
            user_id=role_config.MASTER_ID, user_name=role_config.MASTER_NAME, is_default=True
        )
        self._sessions: Dict[str, SessionRuntime] = {}

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------

    def load(self) -> None:
        """启动时调用一次。如果文件不存在 → 自动从 chat_history 初始化。"""
        if self.file.is_file():
            try:
                raw = json.loads(self.file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                logger.error(f"[IdentityStore] 读 {self.file} 失败, 将重新初始化: {e}")
                raw = {}
        else:
            logger.info(f"[IdentityStore] {self.file.name} 不存在, 稍后从 chat_history 初始化")
            raw = {}

        # default_identity
        di = raw.get("default_identity") or {}
        if di.get("user_id") and di.get("user_name"):
            self._default_identity = Identity(
                user_id=di["user_id"],
                user_name=di["user_name"],
                is_default=True,
            )

        # sessions
        self._sessions = {}
        for sid, payload in (raw.get("sessions") or {}).items():
            try:
                rt = self._parse_session_runtime(payload)
                if rt:
                    self._sessions[sid] = rt
            except Exception as e:
                logger.warning(f"[IdentityStore] 跳过损坏 session runtime {sid}: {e}")

        logger.info(
            f"[IdentityStore] 加载完毕: default={self._default_identity.user_id}/"
            f"{self._default_identity.user_name}, sessions={len(self._sessions)}"
        )

    async def ensure_initialized_from_store(self) -> None:
        """从 SQLite 消息存储（sessions 表）读取所有会话，给没有 runtime 的会话各塞一个默认身份。

        替代旧的 ensure_initialized_from_chat_files（扫 chat_history/*.json）。
        数据源统一为 MessageRepository 的 SQLite。
        """
        from memory.message_repository import get_message_store
        store = get_message_store()

        sessions = await store.list_sessions()

        logger.info(f"[IdentityStore] ensure_initialized_from_store: {len(sessions)} sessions")
        changed = False
        for s in sessions:
            session_id = s.get("session_id", "")
            session_name = s.get("session_name", "")
            m = _SESSION_ID_RE.match(session_id)
            if not m:
                continue
            scope = m.group(1).lower()
            bare_id = m.group(2)
            if bare_id in self._sessions:
                logger.info(f"[IdentityStore]   skip {bare_id}: already has runtime")
                continue

            if scope == "private":
                # 私聊默认 = (session_id, session_name)
                user_id = bare_id
                user_name = session_name or bare_id
            else:
                # 群聊默认 = 全局主人身份（RoleConfig.MASTER_NAME）
                user_id = self._default_identity.user_id
                user_name = self._default_identity.user_name

            seed = Identity(
                user_id=user_id,
                user_name=user_name,
                is_default=True,
                created_at=time.time(),
            )
            self._sessions[bare_id] = SessionRuntime(
                session_id=bare_id,
                scope=scope,
                active_identity=seed,
                identity_lib=[seed],
            )
            logger.info(
                f"[IdentityStore] 自动初始化: {scope}_{bare_id} "
                f"-> ({user_id}, {user_name})"
            )
            changed = True

        if changed:
            self.save()
        logger.info(f"[IdentityStore] ensure done, {len(self._sessions)} sessions in memory")

    # ------------------------------------------------------------------
    # 运行时查询 (无锁, 只读内存)
    # ------------------------------------------------------------------

    def get_runtime(self, session_id: str) -> Optional[SessionRuntime]:
        return self._sessions.get(session_id)

    def get_all_runtimes(self) -> Dict[str, SessionRuntime]:
        return dict(self._sessions)

    def get_default_identity(self) -> Identity:
        return self._default_identity

    # ------------------------------------------------------------------
    # 写 (无锁, 由调用方负责串行)
    # ------------------------------------------------------------------

    def put_runtime(self, rt: SessionRuntime) -> None:
        self._sessions[rt.session_id] = rt

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def save(self) -> None:
        """原子写: 写 .tmp, rename 覆盖。"""
        payload = {
            "version": RUNTIME_VERSION,
            "default_identity": {
                "user_id": self._default_identity.user_id,
                "user_name": self._default_identity.user_name,
            },
            "sessions": {
                sid: {
                    "session_id": rt.session_id,
                    "scope": rt.scope,
                    "active_identity": rt.active_identity.to_dict(),
                    "identity_lib": [i.to_dict() for i in rt.identity_lib],
                }
                for sid, rt in self._sessions.items()
            },
        }
        tmp = self.file.with_suffix(self.file.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.file)
            logger.debug(f"[IdentityStore] 已写 {self.file.name}")
        except OSError as e:
            logger.error(f"[IdentityStore] 写 {self.file} 失败: {e}")

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_session_runtime(payload: dict) -> Optional[SessionRuntime]:
        sid = payload.get("session_id")
        scope = payload.get("scope")
        if not sid or scope not in ("private", "group"):
            return None
        ai = payload.get("active_identity") or {}
        active = Identity(
            user_id=ai.get("user_id", ""),
            user_name=ai.get("user_name", ""),
            is_default=ai.get("is_default", False),
            created_at=ai.get("created_at", 0.0),
        )
        lib: List[Identity] = []
        for entry in (payload.get("identity_lib") or []):
            if not entry.get("user_id") or not entry.get("user_name"):
                continue
            lib.append(Identity(
                user_id=entry["user_id"],
                user_name=entry["user_name"],
                is_default=entry.get("is_default", False),
                created_at=entry.get("created_at", 0.0),
            ))
        return SessionRuntime(
            session_id=sid,
            scope=scope,
            active_identity=active,
            identity_lib=lib,
        )