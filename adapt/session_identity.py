"""One parser for platform-prefixed session IDs and existing legacy IDs."""
from dataclasses import dataclass


PLATFORM_PREFIXES = ("qq", "wechat", "web")
SCOPES = ("private", "group", "channel", "unknown")


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


def session_platform(value: str) -> str:
    """会话的平台前缀；旧格式（无平台前缀）返回空串。"""
    try:
        return parse_session_id(value).platform or ""
    except ValueError:
        return ""


def split_bare(value: str) -> tuple[str, str]:
    """完整 session_id -> (scope, 前端裸 ID)。

    带平台前缀的会话（2026-10-16 起）裸 ID 保留平台与地址，
    例如 'wechat_private_10003' -> ('private', 'wechat_10003')；
    旧的 'private_xxx' / 'group_xxx' 裸 ID 即地址本身。
    """
    first, sep, rest = value.partition("_")
    if sep and first in PLATFORM_PREFIXES:
        scope, sep2, identity = rest.partition("_")
        if sep2 and scope in SCOPES and identity:
            return scope, f"{first}_{identity}"
    if sep and first in ("private", "group") and rest:
        return first, rest
    return "private", value


def join_session_id(scope: str, bare: str) -> str:
    """split_bare 的逆运算：还原成消息库里的完整 session_id。"""
    platform, sep, identity = bare.partition("_")
    if sep and platform in PLATFORM_PREFIXES and identity:
        return f"{platform}_{scope}_{identity}"
    return f"{scope}_{bare}"
