# -*- coding: utf-8 -*-
"""config.env_tools —— 设置文件的环境变量覆盖层

约定：config/*.py 里的字面量是「默认值 + 文档」，同名环境变量（通常来自项目根
.env.dev，由本模块 ensure_env_loaded() 自动加载）优先。只有**部署参数**
（端口 / token / 开关 / API key / 接入点 / 模型名）走这一层；
人格、行为类配置保持纯 Python 字面量，不引入环境变量。

打包 exe 后 config/*.py 会被冻结，届时用户改 exe 旁的 .env 即可换配置。
"""

from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv

from paths import app_root

_ENV_LOADED = False


def ensure_env_loaded() -> None:
    """加载项目根 .env.dev（幂等；已存在的进程环境变量不会被文件覆盖）。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    load_dotenv(app_root() / ".env.dev")
    _ENV_LOADED = True


def env_str(name: str, default: str) -> str:
    """非空字符串配置；环境变量未设置或为空串时用默认值。"""
    ensure_env_loaded()
    value = os.getenv(name)
    return value if value else default


def env_opt(name: str, default: Optional[str]) -> Optional[str]:
    """可空字符串配置（如 Web ACCESS_TOKEN=None 表示不鉴权）；空串视同未设置。"""
    ensure_env_loaded()
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def env_int(name: str, default: int) -> int:
    """整数配置；环境变量非法时抛错（启动期暴露，不静默吞）。"""
    ensure_env_loaded()
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"环境变量 {name}={raw!r} 不是合法整数") from None


def env_bool(name: str, default: bool) -> bool:
    """布尔开关；接受 1/true/yes/on/y（大小写不敏感）。"""
    ensure_env_loaded()
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")
