"""从 design/tokens.json 生成两端产物。

生成物（不要手改）：
  web_frontend/src/tokens.css
  launcher/Theme/Shared.xaml   —— 形态 + 字体（端无关）
  launcher/Theme/Dark.xaml     —— 暗色
  launcher/Theme/Light.xaml    —— 亮色

用法：
  python design/build_tokens.py            # 写入
  python design/build_tokens.py --check    # 只校验是否与源头一致（不一致返回 1）
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "design" / "tokens.json"
CSS_OUT = ROOT / "web_frontend" / "src" / "tokens.css"
WPF_DIR = ROOT / "launcher" / "Theme"

BANNER = "此文件由 design/build_tokens.py 从 design/tokens.json 生成，请勿手改。"

# ---------------------------------------------------------------- 颜色工具

RGBA_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)")


def to_argb(value: str) -> str:
    """把 #RRGGBB / rgba(...) 统一成 WPF 用的 #AARRGGBB。"""
    value = value.strip()
    if value.startswith("#"):
        hexpart = value[1:]
        if len(hexpart) == 3:
            hexpart = "".join(c * 2 for c in hexpart)
        if len(hexpart) == 6:
            return "#FF" + hexpart.upper()
        if len(hexpart) == 8:
            return "#" + hexpart.upper()
        raise ValueError(f"无法解析颜色: {value}")
    m = RGBA_RE.fullmatch(value)
    if m:
        r, g, b = (int(m.group(i)) for i in range(1, 4))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return f"#{round(a * 255):02X}{r:02X}{g:02X}{b:02X}"
    raise ValueError(f"无法解析颜色: {value}")


def is_color(value) -> bool:
    return isinstance(value, str) and bool(RGBA_RE.fullmatch(value.strip()) or re.fullmatch(r"#[0-9a-fA-F]{3,8}", value.strip()))


# ---------------------------------------------------------------- CSS

def pascal(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[-_]", name))


def font_stack_css(names: list[str]) -> str:
    out = []
    for n in names:
        if n.startswith("-") or n in ("sans-serif", "monospace", "ui-monospace", "system-ui"):
            out.append(n)
        elif " " in n:
            out.append(f'"{n}"')
        else:
            out.append(n)
    return ", ".join(out)


def build_css(tokens: dict) -> str:
    form, typo = tokens["form"], tokens["typography"]
    dark, light = tokens["themes"]["dark"], tokens["themes"]["light"]
    # CSS 变量什么值都能装（颜色、阴影、任意字符串），所以除了两份元数据之外**全都要输出**。
    # 早期版本只挑 is_color 的键，结果 shadow-* 从没进过 tokens.css，
    # 所有 var(--shadow-*) 一直静默失效 —— 由 design/check_web_vars.py 抓出来的。
    skip = {"colorScheme"}
    value_keys = [k for k, v in dark.items() if not k.startswith("_") and k not in skip]

    L: list[str] = []
    L.append("/* ============================================================")
    L.append("   Abyss CMF · 设计令牌（自动生成）")
    L.append(f"   {BANNER}")
    L.append("   ============================================================ */")
    L.append("")

    # 形态 + 字体（端无关），放 :root
    L.append(":root {")
    L.append("  /* —— 圆角 —— */")
    for k, v in form["radius"].items():
        L.append(f"  --radius-{k}: {v}px;")
    L.append("")
    L.append("  /* —— 斜切角（锐边/斜切形态语言）—— */")
    for k, v in form["chamfer"].items():
        L.append(f"  --chamfer-{k}: {v}px;")
    L.append("")
    L.append("  /* —— 描边 —— */")
    for k, v in form["borderWidth"].items():
        L.append(f"  --border-{k}: {v}px;")
    L.append("")
    L.append("  /* —— 间距（8px 栅格）—— */")
    for k, v in form["space"].items():
        L.append(f"  --space-{k}: {v}px;")
    L.append("")
    L.append("  /* —— 字体 —— */")
    L.append(f"  --font-sans: {font_stack_css(typo['sans'])};")
    L.append(f"  --font-mono: {font_stack_css(typo['mono'])};")
    for k, v in typo["size"].items():
        L.append(f"  --text-{k}: {v}px;")
    for k, v in typo["weight"].items():
        L.append(f"  --weight-{k}: {v};")
    L.append("}")
    L.append("")

    def color_block(theme: dict, selector: str, comment: str) -> None:
        L.append(f"/* —— {comment} —— */")
        L.append(f"{selector} {{")
        L.append(f"  color-scheme: {theme['colorScheme']};")
        L.append("")
        for k in value_keys:
            L.append(f"  --{k}: {theme[k]};")
        L.append("}")
        L.append("")

    color_block(dark, ":root", "暗色（默认）：CMF 卡背景中性阶梯 + 白字 + 红强调")
    color_block(light, '[data-theme="light"]', "亮色：同一中性系翻转，强调色不变")

    return "\n".join(L).rstrip() + "\n"


# ---------------------------------------------------------------- WPF

SYS_NS = 'xmlns:sys="clr-namespace:System;assembly=System.Runtime"'


def wpf_header(summary: str, extra_ns: str = "") -> list[str]:
    L = [
        f"<!-- {BANNER} -->",
        f"<!-- {summary} -->",
        '<ResourceDictionary xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"',
        '                    xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"',
    ]
    if extra_ns:
        L.append(f"                    {extra_ns}")
    L[-1] += ">"
    return L


def build_wpf_shared(tokens: dict) -> str:
    form, typo = tokens["form"], tokens["typography"]
    L = wpf_header("形态与字体令牌（端无关，主题切换时不重建）", SYS_NS)

    L.append("")
    L.append("  <!-- 圆角 -->")
    for k, v in form["radius"].items():
        L.append(f'  <CornerRadius x:Key="Radius{pascal(k)}">{v}</CornerRadius>')

    L.append("")
    L.append("  <!-- 斜切角 -->")
    for k, v in form["chamfer"].items():
        L.append(f'  <sys:Double x:Key="Chamfer{pascal(k)}">{v}</sys:Double>')

    L.append("")
    L.append("  <!-- 描边 -->")
    for k, v in form["borderWidth"].items():
        L.append(f'  <Thickness x:Key="Border{pascal(k)}">{v}</Thickness>')

    L.append("")
    L.append("  <!-- 间距 -->")
    for k, v in form["space"].items():
        L.append(f'  <sys:Double x:Key="Space{k}">{v}</sys:Double>')
        # Padding 要的是 Thickness，不能拿 Double 顶替，所以另出一套
        L.append(f'  <Thickness x:Key="Pad{k}">{v}</Thickness>')

    L.append("")
    L.append("  <!-- 字体 -->")
    sans = ", ".join(n for n in typo["sans"] if not n.startswith("-"))
    mono = ", ".join(n for n in typo["mono"] if n not in ("ui-monospace",))
    L.append(f'  <FontFamily x:Key="FontSans">{sans}</FontFamily>')
    L.append(f'  <FontFamily x:Key="FontMono">{mono}</FontFamily>')
    for k, v in typo["size"].items():
        L.append(f'  <sys:Double x:Key="FontSize{pascal(k)}">{v}</sys:Double>')
    for k, v in typo["weight"].items():
        L.append(f'  <sys:Double x:Key="FontWeight{pascal(k)}">{v}</sys:Double>')

    L.append("")
    L.append("</ResourceDictionary>")
    return "\n".join(L) + "\n"


def build_wpf_theme(tokens: dict, name: str) -> str:
    theme = tokens["themes"][name]
    keys = [k for k, v in theme.items() if not k.startswith("_") and is_color(v)]
    L = wpf_header(f"{name} 主题颜色令牌", SYS_NS)
    L.append("")
    L.append("  <!-- 颜色（Color 供渐变/动画用，Brush 供直接引用）-->")
    for k in keys:
        argb = to_argb(theme[k])
        p = pascal(k)
        L.append(f'  <Color x:Key="Color{p}">{argb}</Color>')
        L.append(f'  <SolidColorBrush x:Key="Brush{p}" Color="{argb}"/>')
    L.append("")
    L.append("</ResourceDictionary>")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- 主流程

def main() -> int:
    check = "--check" in sys.argv
    tokens = json.loads(SRC.read_text(encoding="utf-8"))

    outputs = {
        CSS_OUT: build_css(tokens),
        WPF_DIR / "Shared.xaml": build_wpf_shared(tokens),
        WPF_DIR / "Dark.xaml": build_wpf_theme(tokens, "dark"),
        WPF_DIR / "Light.xaml": build_wpf_theme(tokens, "light"),
    }

    stale = []
    for path, content in outputs.items():
        if check:
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != content:
                stale.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 统一 LF，避免 Windows 换行导致 --check 永远不一致
            path.write_text(content, encoding="utf-8", newline="\n")
            print(f"  写入 {path.relative_to(ROOT)}")

    if check:
        if stale:
            print("以下生成物与 tokens.json 不一致，请跑 python design/build_tokens.py：")
            for p in stale:
                print(f"  {p.relative_to(ROOT)}")
            return 1
        print("生成物与 tokens.json 一致。")
        return 0

    print(f"完成：{len(outputs)} 个文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
