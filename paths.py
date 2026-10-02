# -*- coding: utf-8 -*-
"""paths —— 统一的项目根路径解析（源码运行 / PyInstaller 冻结运行通吃）

冻结模式下 `__file__` 指向解包目录（onedir 为 *_internal/），任何
`Path(__file__)` 推导项目根都会错位。全项目需要"项目根"的地方一律
import paths → app_root()：
    - 源码运行：paths.py 位于项目根 → app_root() = 项目根
    - 冻结运行：app_root() = 可执行文件所在目录（用户数据与 .env.dev 都在这里）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def ensure_cwd() -> None:
    """把工作目录钉到项目根：代码里有大量 memory/... logs/... 相对路径。

    在每个进程入口（main.py / manager.py / tray.py）调用一次。
    """
    root = app_root()
    try:
        os.chdir(root)
    except OSError:
        pass
