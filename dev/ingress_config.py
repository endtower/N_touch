"""接入进程与 Core 共用的本机接口配置；不启动大脑或模型客户端。"""
from __future__ import annotations

import os
import secrets

from dotenv import dotenv_values
from paths import app_root, data_root


def settings() -> dict:
    candidate = data_root() / ".env.dev"
    if not candidate.is_file():
        candidate = app_root() / ".env.dev"
    values = dict(dotenv_values(candidate)) if candidate.is_file() else {}
    values.update(os.environ)
    return values


def api_port() -> int:
    port = int(settings().get("INGRESS_API_PORT") or 5003)
    if not 1 <= port <= 65535:
        raise ValueError("INGRESS_API_PORT 必须在 1–65535 范围内")
    return port


def service_token() -> str:
    """沿用管理服务令牌；源码单独运行时也能初始化，不输出令牌。"""
    path = data_root() / "manager" / "service_token.txt"
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if not token:
            raise ValueError("管理服务令牌文件为空")
        return token
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(token)
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()
    return token


def onebot_config():
    from adapt.onebot.config import OneBotChannelConfig
    from config.adapt_settings import get_onebot_config
    config = OneBotChannelConfig.from_platform_config(get_onebot_config())
    config.raw_events_dir = str(data_root() / config.raw_events_dir)
    config.media_base_dir = str(data_root() / config.media_base_dir)
    if not 1 <= config.port <= 65535:
        raise ValueError("ONEBOT_WS_PORT 必须在 1–65535 范围内")
    return config


def onebot_enabled() -> bool:
    from config.adapt_settings import is_onebot_enabled
    return is_onebot_enabled()
