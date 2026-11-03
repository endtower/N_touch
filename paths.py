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
    用于定位"随包收集、运行期不写"的资源（如 component_versions.json、旧版兼容 env）。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return app_root()


def web_dist() -> Path:
    """前端活资源目录（web_frontend/dist）。

    前端始终是活资源、不随 exe 打包（2026-10-15）：源码运行 = 仓库根；
    冻结运行 = 数据根（`常用版本/abyss-data-dir.txt` 指向项目根）。
    因此 `npm run build` 后刷新页面即生效，无需重打包、无需复制。
    """
    managed = app_root() / "components" / "core" / "source" / "web_frontend" / "dist"
    if managed.is_dir():
        return managed
    # 源码组件中的 Core 与冻结管理服务必须使用同一份随版本发布的前端。
    local = app_root() / "web_frontend" / "dist"
    if (app_root().parent / "release.json").is_file() and local.is_dir():
        return local
    return data_root() / "web_frontend" / "dist"


def data_root() -> Path:
    """可写用户数据根：数据库 / 日志 / 身份 / 人格 / 媒体等一切运行期可变数据。

    优先使用 ABYSS_DATA_DIR，再使用程序目录的 abyss-data-dir.txt；
    未指定时便携模式默认 = exe 目录（源码运行 = 仓库根）。
    所有可变数据必须经此解析，禁止写入打包资源目录。
    """
    env = os.getenv("ABYSS_DATA_DIR", "").strip()
    if env:
        return Path(env).resolve()
    descriptor = app_root() / "abyss-data-dir.txt"
    if descriptor.is_file():
        configured = descriptor.read_text(encoding="utf-8-sig").strip()
        if not configured:
            raise ValueError("abyss-data-dir.txt 未指定数据目录")
        root = (app_root() / configured).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"指定的数据目录不存在: {root}")
        return root
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
