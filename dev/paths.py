# -*- coding: utf-8 -*-
"""paths —— 统一的项目根路径解析（源码运行 / PyInstaller 冻结运行通吃）

冻结模式下 `__file__` 指向解包目录（onedir 为 *_internal/），任何
`Path(__file__)` 推导项目根都会错位。全项目需要"项目根"的地方一律
import paths → app_root()：
    - 源码运行：paths.py 位于项目根 → app_root() = 项目根
    - 冻结运行：app_root() = 可执行文件所在目录；个人数据单独解析。
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
    source = code_root()
    if source.name == "source" and source.parent.parent.name == "components" and (source.parent / "release.json").is_file():
        return source.parent.parent.parent
    return source


def code_root() -> Path:
    """当前源码模块所在目录，与安装根和个人数据根分别定位。"""
    return Path(__file__).resolve().parent


def managed_install() -> bool:
    return (app_root() / "components/manager/release.json").is_file()


def storage_root() -> Path:
    """安装布局根；受管 Core 与管理器解析到同一个 app 的上级目录。

    标准安装为 Abyss/app，开发工作区直接以仓库为布局根。
    此函数只解析位置，不创建目录、不移动现有资料。
    """
    program = app_root()
    descriptor = program / "abyss-storage-dir.txt"
    if descriptor.is_file():
        value = descriptor.read_text(encoding="utf-8-sig").strip()
        if not value:
            raise ValueError("abyss-storage-dir.txt 未指定布局根")
        return (program / value).resolve()
    if program.name == "source" and (program.parent / "release.json").is_file():
        program = program.parent.parent.parent
    return program.parent if program.name.lower() == "app" else program


def cache_root() -> Path:
    return storage_root() / "cache"


def backup_root() -> Path:
    return storage_root() / "backups"


def external_root() -> Path:
    """第三方安装区；不约束第三方内部的配置或数据存储。"""
    return storage_root() / "external"


def has_legacy_data(root: Path) -> bool:
    """只检查文件存在性；避免旧安装被静默切换到一个空数据目录。"""
    files = any((root / name).is_file() for name in (
        ".env.dev", "memory/memory.db", "memory/chat_history.db", "memory/chat_history/chat_history.db",
        "manager/service_token.txt", "user_settings/system_personas.json",
    ))
    return files or any(any((root / name).glob("*.json")) for name in (
        "brain/base/persona", "brain/amy/persistence", "adapt/web/data",
    ))


def resource_root() -> Path:
    """只读打包资源根（随分发的静态资源）。

    冻结运行 = PyInstaller 解包目录（core_internal）；源码运行 = 仓库根。
    用于定位"随包收集、运行期不写"的资源（如 component_versions.json、旧版兼容 env）。
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", app_root()))
    return code_root()


def web_dist() -> Path:
    """前端活资源目录（web_frontend/dist）。

    优先受管 Core 的版本资源，其次源码根下的构建结果；
    最后兼容旧运行版位于数据根的前端资源。前端构建结果不属于个人数据。
    """
    managed = app_root() / "components" / "core" / "source" / "web_frontend" / "dist"
    if managed.is_dir():
        return managed
    # 源码组件中的 Core 与冻结管理服务必须使用同一份随版本发布的前端。
    local = code_root() / "web_frontend" / "dist"
    if local.is_dir():
        return local
    return data_root() / "web_frontend" / "dist"


def data_root() -> Path:
    """可写用户数据根：数据库 / 日志 / 身份 / 人格 / 媒体等一切运行期可变数据。

    优先使用 ABYSS_DATA_DIR，再使用程序目录的 abyss-data-dir.txt；
    未指定时保留已有旧数据；全新工作区默认 = 布局根/data。
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
    if has_legacy_data(app_root()):
        return app_root()
    return storage_root() / "data"


def log_root() -> Path:
    """日志根：data_root()/logs。日志生产（utils.logger）与网页读取（/system/logs）共用。"""
    return data_root() / "logs"


def ensure_cwd() -> None:
    """把工作目录钉到数据根：代码里仍有少量 cwd 相对路径（config 媒体目录等）。

    在每个进程入口（main.py / manager.py / tray.py）调用一次。
    """
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
