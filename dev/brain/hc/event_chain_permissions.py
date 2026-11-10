"""按主人和目标会话隔离的社交发送权限。"""

import asyncio
from dataclasses import dataclass
from math import ceil
import time


MAX_DURATION_MINUTES = 24 * 60


@dataclass(frozen=True)
class SocialSendPolicy:
    mode: str
    notify: bool = False
    deadline: float | None = None

    @property
    def remaining_seconds(self) -> int | None:
        if self.deadline is None:
            return None
        return max(0, ceil(self.deadline - time.monotonic()))


class EventChainPermissions:
    """一条链一个实例；关闭后，旧 Agent 持有的实例也不能再授权。"""

    def __init__(self, chain_id: str, default_send_mode: str, *, default_notify: bool = False,
                 timed_grants: dict[tuple[str, str], SocialSendPolicy] | None = None):
        if default_send_mode not in ("approval", "unrestricted"):
            raise ValueError(f"非法社交发送默认模式: {default_send_mode!r}")
        self.chain_id = chain_id
        self._default_policy = SocialSendPolicy(default_send_mode, default_send_mode == "unrestricted" and default_notify)
        self._active = True
        self._policies: dict[tuple[str, str], SocialSendPolicy] = {}
        self._timed_grants = timed_grants if timed_grants is not None else {}
        now = time.monotonic()
        for key, policy in list(self._timed_grants.items()):
            if policy.deadline <= now:
                del self._timed_grants[key]
        # 审批共用主人消息游标，必须串行，防止同一回复裁决两个请求。
        self.approval_lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    def require_active(self) -> None:
        if not self._active:
            raise RuntimeError("事件链已结束，社交发送授权已失效")

    @staticmethod
    def _key(master_session_id: str, target_session_id: str) -> tuple[str, str]:
        if not master_session_id or not target_session_id:
            raise ValueError("社交发送权限必须指定主人会话和目标会话")
        return master_session_id, target_session_id

    def get_send_mode(self, master_session_id: str, target_session_id: str) -> str:
        return self.get_send_policy(master_session_id, target_session_id).mode

    def get_send_policy(self, master_session_id: str, target_session_id: str) -> SocialSendPolicy:
        self.require_active()
        key = self._key(master_session_id, target_session_id)
        policy = self._policies.get(key)
        if policy is not None:
            return policy
        timed = self._timed_grants.get(key)
        if timed is not None:
            if timed.deadline > time.monotonic():
                return timed
            del self._timed_grants[key]
        return self._default_policy

    def grant_free_send(self, master_session_id: str, target_session_id: str,
                        approval_seq: int, *, notify: bool = False,
                        duration_minutes: int | None = None) -> SocialSendPolicy:
        """仅由工具层在收到主人真实批准后调用；不作为 LLM 工具暴露。"""
        self.require_active()
        if not isinstance(approval_seq, int) or isinstance(approval_seq, bool) or approval_seq <= 0:
            raise ValueError("自由发送授权缺少主人批准消息序号")
        key = self._key(master_session_id, target_session_id)
        if duration_minutes is None:
            self._timed_grants.pop(key, None)
            policy = SocialSendPolicy("unrestricted", notify)
            self._policies[key] = policy
        else:
            if type(duration_minutes) is not int or not 1 <= duration_minutes <= MAX_DURATION_MINUTES:
                raise ValueError(f"授权时长须为 1 到 {MAX_DURATION_MINUTES} 分钟")
            policy = SocialSendPolicy("unrestricted", notify, time.monotonic() + duration_minutes * 60)
            self._policies.pop(key, None)
            self._timed_grants[key] = policy
        return policy

    def revoke_free_send(self, master_session_id: str, target_session_id: str) -> None:
        self.require_active()
        key = self._key(master_session_id, target_session_id)
        self._timed_grants.pop(key, None)
        self._policies[key] = SocialSendPolicy("approval")

    def close(self) -> None:
        self._active = False
        self._policies.clear()
