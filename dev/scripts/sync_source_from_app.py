"""比对运行版 `app` 与开发工程 `dev` 的源码，并把 `app` 里改好的文件回流到 `dev`。

由来：日常在 `app/components/<组件>/source` 上直接改代码能立刻看到效果，但
`app` 整个被忽略、不进版本库；发布只认 `dev`。两边不自动同步，久了就会分叉。

这个脚本做两件事：
  1. 报出两边内容不同的源码文件，并说明是哪一边更新（按修改时间判断）；
  2. 加 --execute 时，只把「app 更新」的那部分写回 dev，不动其它方向。

「dev 更新」的文件不自动覆盖 app —— 那通常意味着运行版落后于开发版，
应该走构建安装，而不是把新代码悄悄写进正在跑的目录。

不参与比对的：前端编译产物、requirements.lock、release.json、__pycache__。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workspace_layout import APP, SOURCE

# 运行版放源码的两个目录；core 与 manager 有大量共用文件，按相对路径去重。
APP_SOURCES = (APP / "components/core/source", APP / "components/manager/source")

# 运行版里这些文件在 dev 的位置不同，需要显式映射。
PATH_OVERRIDES = {
    "STORAGE.md": "docs/STORAGE.md",
}

SKIP_PARTS = {"__pycache__", ".git", "node_modules"}
SKIP_SUFFIX = (".pyc", ".pyo")
SKIP_PREFIX = ("web_frontend/dist/",)
SKIP_NAMES = {"release.json", "requirements.lock"}


def wanted(relative: str) -> bool:
    if relative in SKIP_NAMES:
        return False
    if relative.endswith(SKIP_SUFFIX):
        return False
    return not relative.startswith(SKIP_PREFIX)


def dev_path_for(relative: str) -> Path:
    return SOURCE / PATH_OVERRIDES.get(relative, relative)


def scan(root: Path) -> dict[str, Path]:
    """列出目录下的候选源码，键是相对该目录的路径。"""
    found: dict[str, Path] = {}
    if not root.is_dir():
        return found
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if name not in SKIP_PARTS]
        for name in files:
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if wanted(relative):
                found[relative] = path
    return found


def mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def compare():
    """返回 (app 更新, dev 更新, 只在新版里存在) 三组相对路径。"""
    app_newer: list[str] = []
    dev_newer: list[str] = []
    missing_in_dev: list[str] = []
    seen: set[str] = set()

    for root in APP_SOURCES:
        for relative, app_file in scan(root).items():
            if relative in seen:
                continue                     # core / manager 共用文件只算一次
            seen.add(relative)
            dev_file = dev_path_for(relative)
            if not dev_file.is_file():
                missing_in_dev.append(relative)
                continue
            if app_file.read_bytes() == dev_file.read_bytes():
                continue
            (app_newer if mtime(app_file) > mtime(dev_file) else dev_newer).append(relative)

    return sorted(app_newer), sorted(dev_newer), sorted(missing_in_dev)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--execute", action="store_true", help="把 app 更新的文件写回 dev；默认只报告")
    args = parser.parse_args()

    app_newer, dev_newer, missing = compare()

    print(f"运行版：{APP}")
    print(f"开发版：{SOURCE}")
    print()

    if app_newer:
        print(f"① 运行版更新，需要回流到 dev（{len(app_newer)} 个）：")
        for relative in app_newer:
            print("   ", relative)
    if missing:
        print()
        print(f"② 只在运行版里有，dev 缺少（{len(missing)} 个）：")
        for relative in missing:
            print("   ", relative)
    if dev_newer:
        print()
        print(f"③ 开发版更新，运行版落后（{len(dev_newer)} 个，不自动覆盖）：")
        for relative in dev_newer:
            print("   ", relative)
        print("    这些应当重新构建并安装，而不是直接写进 app。")

    if not (app_newer or dev_newer or missing):
        print("两边源码完全一致。")
        return 0

    if not args.execute:
        if app_newer or missing:
            print()
            print("这是预演。确认后加 --execute 写入 dev，再用 git 查看差异并提交。")
        return 0

    written = 0
    for relative in app_newer + missing:
        dev_file = dev_path_for(relative)
        for root in APP_SOURCES:
            candidate = root / relative
            if candidate.is_file():
                dev_file.parent.mkdir(parents=True, exist_ok=True)
                dev_file.write_bytes(candidate.read_bytes())
                written += 1
                break

    print()
    print(f"已写入 dev：{written} 个文件。下一步 git status / git diff 检查内容后提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
