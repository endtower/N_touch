# -*- coding: utf-8 -*-
"""
adapt.web.identity_manager
==========================

身份管理业务逻辑。

不变量:
  1. 每个 session 的 identity_lib.size >= 1 (永远)
  2. active_identity 必然在 identity_lib 里 (切换 = 换一个 active 指向)
  3. session 内 (user_id, user_name) 联合唯一
  4. 私聊里允许切换, 但每次切换会附带一个 warning (前端弹一下)
"""

from __future__ import annotations

import asyncio
import time
from typing import List, Optional, Tuple

from utils import setup_logging

from .identity_store import IdentityStore
from .models import Identity, SessionRuntime
from .protocol import ErrorCode

logger = setup_logging("adapt.web.identity_manager")


class IdentityError(Exception):
    """身份操作失败。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class IdentityManager:
    def __init__(self, store: IdentityStore):
        self.store = store
        self._lock = asyncio.Lock()

    # ==========================================================
    # 查询
    # ==========================================================

    def get_runtime(self, session_id: str) -> SessionRuntime:
        rt = self.store.get_runtime(session_id)
        if rt is None:
            raise IdentityError(ErrorCode.SESSION_NOT_FOUND, f"会话 {session_id} 不存在")
        return rt

    # ==========================================================
    # 创建 / 复用
    # ==========================================================

    async def create(
        self,
        session_id: str,
        user_id: str,
        user_name: str,
        switch_to: bool = True,
    ) -> Tuple[SessionRuntime, Optional[str]]:
        """新建身份, 已存在则复用. 返回 (runtime, warning_code_or_none)。

        warning_code 在私聊新建时返回 (前端弹提示)。
        """
        async with self._lock:
            rt = self.get_runtime(session_id)
            self._validate_nonempty(user_id, user_name)

            warning: Optional[str] = None
            if rt.scope == "private":
                warning = "private_create"

            existing = self._find(rt.identity_lib, user_id, user_name)
            if existing is None:
                new_id = Identity(
                    user_id=user_id,
                    user_name=user_name,
                    is_default=False,
                    created_at=time.time(),
                )
                rt.identity_lib.append(new_id)
                logger.info(
                    f"[IdentityManager] +session={session_id} "
                    f"identity=({user_id}, {user_name})"
                )
            else:
                logger.debug(
                    f"[IdentityManager] session={session_id} "
                    f"identity=({user_id}, {user_name}) 已存在, 复用"
                )

            if switch_to:
                target = self._find(rt.identity_lib, user_id, user_name)
                rt.active_identity = target

            self.store.put_runtime(rt)
            self.store.save()
            return rt, warning

    # ==========================================================
    # 更新 (改名 / 改 id)
    # ==========================================================

    async def update(
        self,
        session_id: str,
        old_user_id: str,
        old_user_name: str,
        new_user_id: Optional[str] = None,
        new_user_name: Optional[str] = None,
    ) -> Tuple[SessionRuntime, bool]:
        """返回 (runtime, active_changed).

        active_changed: 如果修改的恰好是 active_identity, 会把 active 指向新的条目并返回 True.
        """
        async with self._lock:
            rt = self.get_runtime(session_id)

            target = self._find(rt.identity_lib, old_user_id, old_user_name)
            if target is None:
                raise IdentityError(
                    ErrorCode.IDENTITY_NOT_FOUND,
                    f"原身份 ({old_user_id}, {old_user_name}) 不存在",
                )

            new_uid = new_user_id or old_user_id
            new_uname = new_user_name or old_user_name
            self._validate_nonempty(new_uid, new_uname)

            if (new_uid, new_uname) != (old_user_id, old_user_name):
                # 检查是否撞了别的条目
                conflict = self._find(rt.identity_lib, new_uid, new_uname)
                if conflict is not None:
                    raise IdentityError(
                        ErrorCode.DUPLICATE_IDENTITY,
                        f"目标身份 ({new_uid}, {new_uname}) 已存在, 无法更新",
                    )

            active_changed = False
            was_active = (
                rt.active_identity.user_id == old_user_id
                and rt.active_identity.user_name == old_user_name
            )

            target.user_id = new_uid
            target.user_name = new_uname

            if was_active:
                rt.active_identity = target
                active_changed = True

            self.store.put_runtime(rt)
            self.store.save()
            logger.info(
                f"[IdentityManager] ~session={session_id} "
                f"({old_user_id}, {old_user_name}) -> ({new_uid}, {new_uname})"
            )
            return rt, active_changed

    # ==========================================================
    # 删除 (最后一个不允许)
    # ==========================================================

    async def delete(
        self,
        session_id: str,
        user_id: str,
        user_name: str,
    ) -> Tuple[SessionRuntime, bool]:
        """返回 (runtime, active_was_deleted).

        active_was_deleted: 如果删的是 active, 自动切到 lib[0], 返回 True.
        """
        async with self._lock:
            rt = self.get_runtime(session_id)

            if len(rt.identity_lib) <= 1:
                raise IdentityError(
                    ErrorCode.LIB_EMPTY_AFTER_DELETE,
                    "身份库至少保留 1 个, 无法继续删除",
                )

            target = self._find(rt.identity_lib, user_id, user_name)
            if target is None:
                raise IdentityError(
                    ErrorCode.IDENTITY_NOT_FOUND,
                    f"身份 ({user_id}, {user_name}) 不存在",
                )

            was_active = (
                rt.active_identity.user_id == user_id
                and rt.active_identity.user_name == user_name
            )
            rt.identity_lib.remove(target)

            if was_active:
                rt.active_identity = rt.identity_lib[0]

            self.store.put_runtime(rt)
            self.store.save()
            logger.info(
                f"[IdentityManager] -session={session_id} "
                f"({user_id}, {user_name})"
            )
            return rt, was_active

    # ==========================================================
    # 切换
    # ==========================================================

    async def switch(
        self,
        session_id: str,
        user_id: str,
        user_name: str,
    ) -> Tuple[SessionRuntime, Optional[str]]:
        """返回 (runtime, warning_code_or_none).

        私聊切换会附带 warning_code='private_switch'.
        """
        async with self._lock:
            rt = self.get_runtime(session_id)
            target = self._find(rt.identity_lib, user_id, user_name)
            if target is None:
                raise IdentityError(
                    ErrorCode.IDENTITY_NOT_FOUND,
                    f"身份 ({user_id}, {user_name}) 不存在",
                )

            warning: Optional[str] = None
            if rt.scope == "private":
                warning = "private_switch"

            rt.active_identity = target
            self.store.put_runtime(rt)
            self.store.save()
            logger.info(
                f"[IdentityManager] *session={session_id} "
                f"active=({user_id}, {user_name})"
            )
            return rt, warning

    # ==========================================================
    # helpers
    # ==========================================================

    @staticmethod
    def _find(lib: List[Identity], user_id: str, user_name: str) -> Optional[Identity]:
        for i in lib:
            if i.user_id == user_id and i.user_name == user_name:
                return i
        return None

    @staticmethod
    def _validate_nonempty(user_id: str, user_name: str) -> None:
        if not (user_id and user_id.strip()):
            raise IdentityError("invalid_identity", "user_id 不能为空")
        if not (user_name and user_name.strip()):
            raise IdentityError("invalid_identity", "user_name (角色名) 不能为空")