#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts.web_only
================

Web 前端独立启动器（只读模式）。

只启动 adapt.web.WebChannel（前端页面 + 查看 API），不启动 SystemCore（大脑），
适合"不跑 LLM、只想查看上下文/记忆/任务/会话记录"的场景：

    python scripts/web_only.py                 # 按配置端口启动(默认 5002)，自动开浏览器
    python scripts/web_only.py --no-browser    # 不自动打开浏览器
    python scripts/web_only.py --port 5003     # 临时换端口（与完整版并存时用）

与 main.py 的关系：
    - 共享同一套数据文件（chat_history.db / memory.db / event_chains / token_stats.json），
      SQLite 为 WAL 模式，本进程几乎只读，互不干扰；
    - 但两者都占用同一端口（配置里 web.WS_PORT），先启者得，后启者报
      "端口被占用"。要换着跑：先 Ctrl+C 停掉当前这个，再启动另一个
      （用 --port 错开端口可以并存，见下一条）；
    - 用 --port 错开端口与完整版并存时：查看全部安全；但不要编辑
      /amy/* 想法池和 WS 测试预设——两个进程各持一份内存态，
      都写 adapt/web 与 brain/amy 下的同名 JSON，后写者覆盖先写者；
      聊天消息的删改写同一 chat_history.db，偶发 database is locked
      属正常，重试即可；
    - 情绪面板可以看：/amy/state 首次访问会惰性加载本地 JSON 单例，
      但本进程里 amy 不会被大脑驱动，状态静止不动属正常。
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import os
import sys
import webbrowser
from pathlib import Path

# 修正 cwd 到项目根：代码里有大量 memory/... logs/... 相对路径，
# 必须与"在项目根目录跑 python main.py"时的环境一致
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

# .env.dev 存在则加载（与 main.py 行为一致；不存在则跳过）
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env.dev")
except ImportError:
    pass


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Abyss Web 只读模式启动器")
    parser.add_argument("--port", type=int, default=None,
                        help="覆盖配置里的端口（默认取 adapt_settings 的 web.WS_PORT）")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动后不自动打开浏览器")
    return parser.parse_args()


def _is_addr_in_use(e: OSError) -> bool:
    if getattr(e, "errno", None) == errno.EADDRINUSE:
        return True
    # Windows: WinError 10048(被占用) / 10013(权限或保留段)
    return "10048" in str(e) or "10013" in str(e)


def main() -> int:
    args = _parse_args()

    from config.adapt_settings import get_web_config
    from adapt.web import WebChannel, WebChannelConfig

    cfg = WebChannelConfig.from_platform_config(get_web_config())
    if args.port:
        cfg.port = args.port

    channel = WebChannel(cfg)
    url = f"http://{cfg.host}:{cfg.port}/"

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(channel.start())
    except OSError as e:
        if _is_addr_in_use(e):
            print(f"\n[X] 启动失败: 端口 {cfg.port} 已被占用。")
            print("    可能是完整版 (main.py) 或另一个 web_only 正在运行。")
            print(f"    请先停掉占用端口的进程，或用 --port 换一个端口。")
            return 1
        raise

    print("=" * 60)
    print("  Abyss Web · 只读模式 (web_only)")
    print(f"  前端 + API : {url}")
    print(f"  数据目录   : {PROJECT_ROOT}")
    print(f"  注意: 与完整版 (main.py) 共用端口 {cfg.port}，二选一运行")
    print("  Ctrl+C 退出")
    print("=" * 60, flush=True)

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            loop.run_until_complete(channel.stop())
        except Exception:
            pass
        loop.close()
        print("\n[web_only] 已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
