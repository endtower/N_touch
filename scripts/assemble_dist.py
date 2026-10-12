#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts.assemble_dist —— 组装 Abyss 便携包（staging 输出，白名单数据）

PyInstaller 构建完成后运行：
    python scripts/assemble_dist.py

产出 dist/staging/Abyss/：
    core.exe / manager.exe        两个冻结入口
    core_internal/                core 运行时（只含二进制依赖与打包资源）
    manager_internal/             管家运行时
    .env.dev / .env.example       配置（真 key 只在 .env.dev，本地私有）
    brain/base/persona/*.json     人格种子数据（data_root 种子，用户可编辑）
    SnowLuma/                     QQ 登录工具（整目录拷贝）

数据安全约定（GPT 审查"重打包与用户数据必须隔离"）：
- 输出永远是全新 staging 目录，绝不删除/覆盖已安装运行目录——安装/更新
  由管家或人工把 staging 内容复制到运行目录，用户数据（memory/、logs/、
  adapt/web/data/、brain/amy/persistence/ 等）留在运行目录不动。
- 白名单原则：只打包明确列出的数据种子，不做"整树复制"；运行期可变数据
  一律经 paths.data_root() 写到 exe 目录（或 ABYSS_DATA_DIR），不进
  core_internal。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STAGING = ROOT / "dist" / "staging" / "Abyss"

EXCLUDE_DIR_NAMES = {"__pycache__", ".git", ".venv", "venv", "node_modules", "logs"}


def copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        print(f"  [跳过] {src} 不存在")
        return
    for item in src.rglob("*"):
        if any(part in EXCLUDE_DIR_NAMES for part in item.relative_to(src).parts):
            continue
        target = dst / item.relative_to(src)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def main() -> int:
    names = ("core", "manager", "ingress")
    if any(not (ROOT / "dist" / name / f"{name}.exe").is_file()
           or not (ROOT / "dist" / name / f"{name}_internal").is_dir() for name in names):
        print("请先构建三个后台入口（用户仍只打开一个 WPF 启动器）：")
        for name in names:
            print(f"  python -m PyInstaller --noconfirm {name}.spec")
        return 1
    if STAGING.exists():
        if not STAGING.resolve().is_relative_to((ROOT / "dist" / "staging").resolve()):
            raise ValueError("staging 路径越界，拒绝删除")
        if any((STAGING / name).exists() for name in ("memory", "logs", "manager")):
            raise ValueError("staging 内存在运行数据，拒绝清空；请先保存或改用新目录")
        print(f"清空旧 staging: {STAGING}")
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)

    print("1) 复制 exe 与运行时目录...")
    for name in names:
        shutil.copy2(ROOT / "dist" / name / f"{name}.exe", STAGING / f"{name}.exe")
        shutil.copytree(ROOT / "dist" / name / f"{name}_internal", STAGING / f"{name}_internal")

    print("2) 复制配置（.env.dev 含真 key，本地私有，勿外传）...")
    for f in (".env.dev", ".env.example"):
        if (ROOT / f).exists():
            shutil.copy2(ROOT / f, STAGING / f)

    print("3) 数据种子白名单（人格档案 → data_root 种子位置）...")
    persona_src = ROOT / "brain" / "base" / "persona"
    persona_dst = STAGING / "brain" / "base" / "persona"
    persona_dst.mkdir(parents=True, exist_ok=True)
    if persona_src.exists():
        for f in persona_src.glob("*.json"):
            shutil.copy2(f, persona_dst / f.name)
    print(f"   人格档案: {len(list(persona_dst.glob('*.json')))} 个")

    print("4) 复制 SnowLuma（整目录，含 node 运行时与登录数据）...")
    copy_tree(ROOT / "SnowLuma", STAGING / "SnowLuma")

    size_mb = sum(f.stat().st_size for f in STAGING.rglob("*") if f.is_file()) // 1048576
    print(f"\n完成: {STAGING}  (~{size_mb} MB)")
    print("安装: 将 staging 内容复制到运行目录（保留该目录下已有 memory/ logs/ 等用户数据）")
    print("使用: 通过 AbyssLauncher.exe 管理 Core、消息接入与 SnowLuma")
    return 0


if __name__ == "__main__":
    sys.exit(main())
