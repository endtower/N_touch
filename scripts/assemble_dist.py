#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts.assemble_dist —— 组装 Abyss 便携包

PyInstaller 构建完成后运行：
    python scripts/assemble_dist.py

产出 dist/Abyss/：
    core.exe / manager.exe        两个冻结入口
    core_internal/                core 运行时（内含源码镜像，见下）
    manager_internal/             管家运行时
    .env.dev / .env.example       配置（真 key 只在 .env.dev，本地私有）
    SnowLuma/                     QQ 登录工具（整目录拷贝）

为什么 core_internal 里要有源码镜像：
    代码大量用 `Path(__file__)` 推导项目根（amy 持久化、persona 目录、
    memory 数据库等）。PyInstaller 6 onedir 把纯 Python 模块收进 PYZ，
    core_internal 下并不存在源码目录结构——冻结运行时这些路径全部失效。
    组装时把 brain/adapt/utils/config/paths.py 镜像进去，让 __file__ 路径
    原样成立；同时天然兜底一切分析器漏掉的动态导入。
    排除项：__pycache__、私有运行时数据（persistence/notes）、neo4j 引擎、
    memory 的重数据（DB / 媒体，运行时自建空库）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "dist" / "Abyss"

EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".venv", "venv", "node_modules"}
# brain 下排除的私有/巨型子目录（相对 brain/ 的路径片段）
BRAIN_EXCLUDE_PARTS = ("amy/persistence", "hc/shortlight/notes", "hc/database/neo4j")


def copy_tree(src: Path, dst: Path, exclude_parts: tuple = ()) -> None:
    if not src.exists():
        print(f"  [跳过] {src} 不存在")
        return
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        if any(part in EXCLUDE_DIR_NAMES for part in rel.parts):
            continue
        if any(str(rel).replace("\\", "/").startswith(p) for p in exclude_parts):
            continue
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def main() -> int:
    if not (ROOT / "dist" / "core" / "core.exe").exists():
        print("请先运行两次 PyInstaller 构建（core / manager）")
        return 1
    if PKG.exists():
        shutil.rmtree(PKG)
    PKG.mkdir(parents=True)

    print("1) 复制 exe 与运行时目录...")
    for name in ("core", "manager"):
        shutil.copy2(ROOT / "dist" / name / f"{name}.exe", PKG / f"{name}.exe")
        shutil.copytree(ROOT / "dist" / name / f"{name}_internal", PKG / f"{name}_internal")

    print("2) 复制配置（.env.dev 含真 key，本地私有，勿外传）...")
    for f in (".env.dev", ".env.example"):
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, PKG / f)

    print("3) 源码镜像进 core_internal（让 __file__ 路径成立）...")
    internal = PKG / "core_internal"
    for pkg in ("brain", "adapt", "utils", "config"):
        copy_tree(ROOT / pkg, internal / pkg, exclude_parts=BRAIN_EXCLUDE_PARTS if pkg == "brain" else ())
    shutil.copy2(ROOT / "paths.py", internal / "paths.py")
    # memory 包只镜像代码；DB/媒体/向量等运行时数据由冻结实例自建空库
    (internal / "memory").mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "memory").glob("*.py"):
        shutil.copy2(f, internal / "memory" / f.name)

    print("4) 复制 SnowLuma（整目录，含 node 运行时）...")
    copy_tree(ROOT / "SnowLuma", PKG / "SnowLuma")

    size_mb = sum(f.stat().st_size for f in PKG.rglob("*") if f.is_file()) // 1048576
    print(f"\n完成: {PKG}  (~{size_mb} MB)")
    print("使用: 双击 manager.exe → 托盘自动拉起 Core → 浏览器打开 http://127.0.0.1:5002")
    return 0


if __name__ == "__main__":
    sys.exit(main())
