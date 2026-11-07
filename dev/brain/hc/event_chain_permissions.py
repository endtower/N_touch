"""事件链内的社交发送权限；只驻留内存，不从历史快照恢复。"""

import asyncio


class EventChainPermissions:
    """一条链一个实例；关闭后，旧 Agent 持有的实例也不能再授权。"""

    def __init__(self, chain_id: str, default_send_mode: str):
        if default_send_mode not in ("approval", "unrestricted"):
            raise ValueError(f"非法社交发送默认模式: {default_send_mode!r}")
        self.chain_id = chain_id
        self._default_send_mode = default_send_mode
        self._active = True
        self._modes: dict[tuple[str, str], str] = {}
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
        self.require_active()
        key = self._key(master_session_id, target_session_id)
        return self._modes.get(key, self._default_send_mode)

    def grant_free_send(self, master_session_id: str, target_session_id: str,
                        approval_seq: int) -> None:
        """仅由工具层在收到主人真实批准后调用；不作为 LLM 工具暴露。"""
        self.require_active()
        if not isinstance(approval_seq, int) or isinstance(approval_seq, bool) or approval_seq <= 0:
            raise ValueError("自由发送授权缺少主人批准消息序号")
        self._modes[self._key(master_session_id, target_session_id)] = "unrestricted"

    def revoke_free_send(self, master_session_id: str, target_session_id: str) -> None:
        self.require_active()
        self._modes[self._key(master_session_id, target_session_id)] = "approval"

    def close(self) -> None:
        self._active = False
        self._modes.clear()
