"""校验启动器 XAML 里引用的资源键是否都存在。

为什么需要这个：WPF 的 DynamicResource 在键不存在时不报错、不崩溃，
只是渲染成 null（字看不到、底色丢失）。构建也一样过。
所以"编译通过"不等于"样式生效"，必须单独查一遍。

用法：python design/check_theme_keys.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "launcher"
THEME = LAUNCHER / "Theme"

XAML_FILES = sorted(LAUNCHER.rglob("*.xaml"))
# 生成物 + 控件样式表都算"键的提供方"
KEY_SOURCES = [THEME / "Shared.xaml", THEME / "Dark.xaml", THEME / "Light.xaml", THEME / "Controls.xaml"]

KEY_RE = re.compile(r'x:Key="([^"]+)"')
# {DynamicResource Foo} / {StaticResource Foo}；StaticResource 允许后面带逗号参数
REF_RE = re.compile(r'\{(?:Dynamic|Static)Resource\s+([A-Za-z0-9_.]+)')


def provided_keys() -> set[str]:
    keys: set[str] = set()
    for path in KEY_SOURCES:
        if not path.exists():
            print(f"！缺少资源文件：{path.relative_to(ROOT)}")
            continue
        keys |= set(KEY_RE.findall(path.read_text(encoding="utf-8")))
    return keys


def main() -> int:
    keys = provided_keys()
    print(f"资源字典提供 {len(keys)} 个键（Shared / Dark / Light / Controls）")

    problems = 0
    for path in XAML_FILES:
        if path.parent == THEME and path.name == "Controls.xaml":
            # 控件表自己内部也互相引用，同样要查，但它的键来自自身
            pass
        text = path.read_text(encoding="utf-8")
        local_keys = set(KEY_RE.findall(text))
        for key in sorted(set(REF_RE.findall(text))):
            if key in keys or key in local_keys:
                continue
            print(f"  ✗ {path.relative_to(ROOT)}: 引用了不存在的键 {key}")
            problems += 1

    if problems:
        print(f"\n有 {problems} 处引用取不到值 —— 这些控件会静默地渲染成空色。")
        return 1
    print("\n所有资源引用都能解析。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
