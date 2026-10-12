"""校验网页侧引用的 CSS 变量是否都有定义。

为什么需要它：网页这边引用一个不存在的 `var(--x)` 不会报错、不会崩，
只是那条样式**静默失效**（颜色/圆角悄悄不生效），构建也一样过。
这和 WPF 侧 `DynamicResource` 找不到键是同一个坑 ——
`design/check_theme_keys.py` 管 WPF，这个脚本管网页，两边都要有人守着。

用法：python design/check_web_vars.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "web_frontend" / "src"
TOKENS = SRC / "tokens.css"

# 引用：var(--name) / var(--name, fallback)
USE_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")
# 定义： --name: value;  —— 也认模板里内联写的（ style="--x: …" / '--x': … ）
DEF_RE = re.compile(r"(--[A-Za-z0-9_-]+)['\"]?\s*:")
# Element Plus 自己会声明这一族变量，不归我们管
EP_PREFIX = "--el-"

SCAN_SUFFIXES = {".vue", ".css", ".ts", ".scss", ".html"}


def defined_in_tokens() -> set[str]:
    if not TOKENS.exists():
        print(f"！缺少 {TOKENS.relative_to(ROOT)}（先跑 python design/build_tokens.py）")
        return set()
    return set(DEF_RE.findall(TOKENS.read_text(encoding="utf-8")))


def main() -> int:
    tokens = defined_in_tokens()
    print(f"tokens.css 定义了 {len(tokens)} 个变量")

    used: dict[str, list[tuple[Path, int]]] = {}
    for path in sorted(SRC.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        if path == TOKENS:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name in USE_RE.findall(text):
            used.setdefault(name, []).append((path, text[: text.index(name)].count("\n") + 1))

    # 本地定义也算数（组件里偶尔会自己声明变量）
    local: set[str] = set()
    for path in sorted(SRC.rglob("*")):
        if path.is_file() and path.suffix in SCAN_SUFFIXES:
            local |= set(DEF_RE.findall(path.read_text(encoding="utf-8", errors="replace")))

    known = tokens | local
    missing = {k: v for k, v in used.items() if k not in known and not k.startswith(EP_PREFIX)}

    if missing:
        print(f"\n有 {len(missing)} 个变量被引用但没有定义 —— 这些样式会静默失效：")
        for name, spots in sorted(missing.items()):
            where = ", ".join(f"{p.relative_to(ROOT)}:{ln}" for p, ln in spots[:3])
            more = f" 等 {len(spots)} 处" if len(spots) > 3 else ""
            print(f"  ✗ {name}  ← {where}{more}")
        return 1

    print(f"引用到的变量共 {len(used)} 个，全部有定义。")
    unused = sorted(t for t in tokens if t not in used and not t.startswith("--el-"))
    if unused:
        print(f"\n提示：tokens.css 里有 {len(unused)} 个变量目前没人用（不是错误，只是可能已废弃）:")
        for name in unused[:15]:
            print(f"  · {name}")
        if len(unused) > 15:
            print(f"  … 另有 {len(unused) - 15} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
