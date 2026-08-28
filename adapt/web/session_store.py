# -*- coding: utf-8 -*-
"""
adapt.web.session_store
=======================

SessionStore：把 `memory/chat_history/*.json` 当作「会话」的真实数据源。

设计原则：
- **会话就是文件**: 每个 `private_XXX.json` / `group_XXX.json` = 一个会话
- **创建会话 = 写新文件**: 没有运行时注册表，文件在就在，删了就没
- **聊天记录 = 文件里的 message_list**: 直接读
- **WebSession 字段推导自文件**:
    session_id     = 文件名裸 ID（不含 scope 前缀），如 RoleConfig.MASTER_ID
    scope          = 文件名前缀（private / group）
    user_id        = 同 session_id（私聊是对方 QQ；群聊是群号）
    user_name      = 优先用 session_info.session_name；空则用 message_list 第一条 user_name
    session_name   = session_info.session_name 或 "<scope>_<id>"
    role           = None（群聊时由用户在 WebChannel 重新指定）
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import setup_logging

from .models import WebSession

logger = setup_logging("adapt.web.session_store")


_FILE_NAME_RE = re.compile(r"^(private|group)_(.+)\.json$", re.IGNORECASE)


class SessionStore:
    """扫描 chat_history 目录，读取 / 写入会话文件。"""

    def __init__(self, chat_history_dir: Path):
        self.dir = Path(chat_history_dir)
        if not self.dir.exists():
            self.dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 列举
    # ------------------------------------------------------------------

    def list_files(self) -> List[Path]:
        """列出所有合法会话文件（跳过 media/ 子目录）。"""
        if not self.dir.is_dir():
            return []
        out: List[Path] = []
        for p in self.dir.iterdir():
            if not p.is_file():
                continue
            if not _FILE_NAME_RE.match(p.name):
                continue
            out.append(p)
        # 按 last_update_time 倒序（最新在最上面）
        out.sort(key=lambda p: self._safe_mtime(p), reverse=True)
        return out

    def list_sessions(self) -> List[WebSession]:
        """列出所有会话（不加载消息，只构造 WebSession 头）。"""
        out: List[WebSession] = []
        for p in self.list_files():
            sess = self._file_to_session_header(p)
            if sess is not None:
                out.append(sess)
        return out

    # ------------------------------------------------------------------
    # 读取 / 解析
    # ------------------------------------------------------------------

    def file_to_session(self, file_path: Path) -> Tuple[WebSession, List[Dict[str, Any]]]:
        """返回 (WebSession, message_list)。失败抛 ValueError。"""
        path = Path(file_path)
        sess = self._file_to_session_header(path)
        if sess is None:
            raise ValueError(f"不是合法会话文件: {path}")
        msgs = self._load_message_list(path)
        return sess, msgs

    def load_messages(self, scope: str, bare_id: str) -> List[Dict[str, Any]]:
        """按 (scope, bare_id) 加载消息列表。文件不存在返回 []。"""
        path = self._file_path_for(scope, bare_id)
        if not path.is_file():
            return []
        return self._load_message_list(path)

    def delete_message(self, scope: str, bare_id: str, message_id: str) -> bool:
        """从文件中删除指定 message_id 的那条消息.

        返回:
          True  - 文件存在且该 message_id 被找到并删除
          False - 文件不存在或 message_id 未找到
        """
        if not message_id:
            return False
        path = self._file_path_for(scope, bare_id)
        if not path.is_file():
            return False
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[SessionStore] 读 {path} 失败: {e}")
            return False
        msgs = raw.get("message_list") or []
        before = len(msgs)
        new_msgs = [m for m in msgs if (m.get("message_id") or "") != message_id]
        if len(new_msgs) == before:
            return False  # 未找到
        raw["message_list"] = new_msgs
        raw["session_info"]["last_update_time"] = int(time.time())
        try:
            # 原子写: tmp -> rename
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError as e:
            logger.warning(f"[SessionStore] 写 {path} 失败: {e}")
            return False
        logger.info(f"[SessionStore] 删除消息: {scope}_{bare_id}/{message_id} (剩 {len(new_msgs)} 条)")
        return True

    # ------------------------------------------------------------------
    # 创建 / 写
    # ------------------------------------------------------------------

    def create_empty(
        self,
        scope: str,
        bare_id: str,
        user_name: str = "",
        session_name: str = "",
        role: Optional[str] = None,
    ) -> WebSession:
        """创建一个新的空会话文件。如果已存在, 直接返回其头。"""
        if scope not in ("private", "group"):
            raise ValueError(f"scope 必须是 private/group，得到 {scope!r}")
        path = self._file_path_for(scope, bare_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file():
            payload = {
                "session_info": {
                    "session_id": f"{scope}_{bare_id}",
                    "session_name": session_name or user_name or f"{scope}_{bare_id}",
                    "last_update_time": int(time.time()),
                    "remark": "",
                },
                "message_list": [],
            }
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.info(f"[SessionStore] 新建空文件: {path}")
        return self._file_to_session_header(path) or WebSession(
            session_id=bare_id, scope=scope, user_id=bare_id, user_name=user_name,
            session_name=session_name or user_name, role=role,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _file_path_for(self, scope: str, bare_id: str) -> Path:
        return self.dir / f"{scope}_{bare_id}.json"

    @staticmethod
    def _safe_mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    def _file_to_session_header(self, path: Path) -> Optional[WebSession]:
        """读文件, 构造 WebSession。不读 message_list。"""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[SessionStore] 跳过损坏文件 {path}: {e}")
            return None

        m = _FILE_NAME_RE.match(path.name)
        if not m:
            return None
        scope = m.group(1).lower()
        bare_id = m.group(2)

        info = raw.get("session_info") or {}
        msgs = raw.get("message_list") or []

        user_name = (
            info.get("session_name")
            or (msgs[0].get("user_name") if msgs else "")
            or bare_id
        )
        session_name = info.get("session_name") or user_name

        return WebSession(
            session_id=bare_id,
            scope=scope,
            user_id=bare_id,
            user_name=user_name,
            session_name=session_name,
            role=None,
            created_at=float(info.get("last_update_time") or self._safe_mtime(path)),
        )

    def _load_message_list(self, path: Path) -> List[Dict[str, Any]]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"[SessionStore] 读 {path} 失败: {e}")
            return []
        return list(raw.get("message_list") or [])
