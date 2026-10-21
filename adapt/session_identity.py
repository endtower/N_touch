"""One parser for platform-prefixed session IDs and existing legacy IDs."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionIdentity:
    platform: str | None
    scope: str
    identity: str


def parse_session_id(value: str) -> SessionIdentity:
    if not isinstance(value, str):
        raise ValueError("会话 ID 必须为字符串")
    first, sep, rest = value.partition("_")
    platform = None
    if first in ("qq", "wechat", "web"):
        platform = first
        first, sep, rest = rest.partition("_")
    if not sep or first not in ("private", "group", "channel", "unknown") or not rest:
        raise ValueError("会话 ID 格式非法")
    return SessionIdentity(platform, first, rest)


def session_scope(value: str) -> str:
    try:
        return parse_session_id(value).scope
    except ValueError:
        return ""
