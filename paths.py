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


def resource_root() -> Path:
    """只读打包资源根（随分发的静态资源）。

    冻结运行 = PyInstaller 解包目录（core_internal）；源码运行 = 仓库根。
    用于定位"随包收集、运行期不写"的资源（如前端 dist、旧版兼容 env）。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


def data_root() -> Path:
    """可写用户数据根：数据库 / 日志 / 身份 / 人格 / 媒体等一切运行期可变数据。

    便携模式默认 = exe 目录（源码运行 = 仓库根，行为与历史完全一致）；
    ABYSS_DATA_DIR 环境变量可显式覆盖（安装器 / 多实例场景）。
    所有可变数据必须经此解析，禁止写入打包资源目录。
    """
    env = os.getenv("ABYSS_DATA_DIR", "").strip()
    if env:
        return Path(env).resolve()
    return app_root()


def log_root() -> Path:
    """日志根：data_root()/logs。日志生产（utils.logger）与网页读取（/system/logs）共用。"""
    return data_root() / "logs"


def ensure_cwd() -> None:
    """把工作目录钉到数据根：代码里仍有少量 cwd 相对路径（config 媒体目录等）。

    在每个进程入口（main.py / manager.py / tray.py）调用一次。
    """
    root = data_root()
    try:
        os.chdir(root)
    except OSError:
        pass
