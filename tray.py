#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tray.py —— Abyss 管家托盘（打包为 manager.exe 的默认入口）

系统托盘常驻：菜单管理 Core / SnowLuma 的启停，一键打开网页系统面板、
前端、SnowLuma WebUI。启动时自动拉起 Core（AUTO_START_CORE 可关）。

入口分派（GPT 审查-2）：manager.exe 无参数 → run_tray()；带参数
（如 `manager.exe respawn core`）→ manager.py 的 CLI 一次性命令。
单实例：占用本地 49899 端口作为锁，第二个实例弹窗提示后退出。
退出行为：菜单「退出」停止全部托管进程（数据由 SQLite WAL 保护）。
"""

from __future__ import annotations

import socket
import sys
import threading
import traceback
import webbrowser

from paths import app_root, ensure_cwd

app_root()
ensure_cwd()
sys.path.insert(0, str(app_root()))

from core.supervisor import build_specs, spec_alive   # noqa: E402
from manager import Manager                           # noqa: E402

LOCK_PORT = 49899
AUTO_START_CORE = True

# run_tray() 注入；菜单处理器经模块全局访问
mgr: Manager | None = None
_specs = None


def _msgbox(text: str, title: str = "Abyss 管家") -> None:
    """无控制台环境下的提示框。"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x40)   # MB_ICONINFORMATION
    except Exception:
        pass


def _tray_icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((6, 6, 58, 58), radius=14, fill=(30, 41, 59, 255),
                        outline=(129, 140, 248, 255), width=3)
    d.text((32, 28), "A", fill=(165, 180, 252, 255), anchor="mm")
    d.ellipse((26, 40, 38, 46), fill=(52, 211, 153, 255))
    return img


def _safe(fn):
    """菜单动作兜底：转发 pystray 传入的 (icon, item)，任何异常弹窗告知。

    修复（GPT 审查-3）：旧版 wrapper 调用 fn() 丢参数，导致需要 icon/item
    的处理器（如退出）必然 TypeError，菜单动作静默失效。
    """
    def wrapper(icon=None, item=None):
        try:
            fn(icon, item)
        except Exception:
            err = traceback.format_exc(limit=3)
            _msgbox(f"操作失败：\n{err}")
    return wrapper


def _core_running() -> bool:
    return spec_alive(_specs["core"])


def _snowluma_running() -> bool:
    return spec_alive(_specs["snowluma"])


@_safe
def _start_core(icon=None, item=None) -> None:
    print(mgr.start("core"), flush=True)


@_safe
def _stop_core(icon=None, item=None) -> None:
    print(mgr.stop("core"), flush=True)


@_safe
def _start_snowluma(icon=None, item=None) -> None:
    print(mgr.start("snowluma"), flush=True)


@_safe
def _stop_snowluma(icon=None, item=None) -> None:
    print(mgr.stop("snowluma"), flush=True)


@_safe
def _open_panel(icon=None, item=None) -> None:
    web_port = _specs["core"].ports[0]
    webbrowser.open(f"http://127.0.0.1:{web_port}/")


@_safe
def _open_snowluma(icon=None, item=None) -> None:
    from core.supervisor import snowluma_webui_port
    webbrowser.open(f"http://127.0.0.1:{snowluma_webui_port()}")


@_safe
def _exit(icon, item) -> None:
    try:
        print("\n".join(mgr.stop_all()), flush=True)
    finally:
        icon.stop()


def _single_instance_lock() -> socket.socket | None:
    try:
        lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock.bind(("127.0.0.1", LOCK_PORT))
        lock.listen(1)
        return lock
    except OSError:
        return None


def run_tray(manager: Manager) -> int:
    """托盘主循环（manager.py 入口分派调用；也可 python tray.py 直接调试）。"""
    global mgr, _specs
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    lock = _single_instance_lock()
    if lock is None:
        _msgbox("Abyss 管家已经在运行了（托盘图标在右下角）。")
        return 0

    mgr = manager
    _specs = mgr.specs

    import pystray

    menu = pystray.Menu(
        pystray.MenuItem("打开系统面板（网页）", _open_panel, default=True),
        pystray.MenuItem("打开前端（同面板地址）", _open_panel),
        pystray.MenuItem("打开 SnowLuma WebUI", _open_snowluma,
                         enabled=lambda item: _snowluma_running()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("启动 Core", _start_core, enabled=lambda item: not _core_running()),
        pystray.MenuItem("停止 Core", _stop_core, enabled=lambda item: _core_running()),
        pystray.MenuItem("启动 SnowLuma", _start_snowluma, enabled=lambda item: not _snowluma_running()),
        pystray.MenuItem("停止 SnowLuma", _stop_snowluma, enabled=lambda item: _snowluma_running()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出（停止全部）", _exit),
    )

    def setup(icon) -> None:
        icon.visible = True
        mgr.start_watchdog()   # 修复（GPT 审查-3）：托盘此前未启用看门狗
        if AUTO_START_CORE:
            threading.Thread(target=_safe(_start_core), daemon=True).start()

    print("Abyss 管家托盘启动", flush=True)
    pystray.Icon("abyss_manager", _tray_icon(), "Abyss 管家", menu).run(setup=setup)
    return 0


if __name__ == "__main__":
    sys.exit(run_tray(Manager()))
