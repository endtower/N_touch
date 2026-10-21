#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts.onebot_check —— OneBot 反向 WS 服务端自检

不启动大脑，单独拉起 OneBotChannel，模拟 SnowLuma/NapCat 客户端做三项检查：
  1. 正确 token（query + Bearer 同时携带，同 SnowLuma 行为）→ 应被接受
  2. 错误 token → 应被 401 拒绝
  3. 发送 lifecycle + group message 事件 → 不应崩溃（无大脑时消息被安全丢弃）

用途：配置 SnowLuma 连接前先确认 Core 侧就绪（见 docs/setup_snowluma.md）；
排查「为什么连不上」时先跑本脚本，把问题二分为 Core 侧 / SnowLuma 侧。

用法：
    python scripts/onebot_check.py              # 用 .env.dev / config 里的端口与 token
    python scripts/onebot_check.py --port 5013  # 临时换端口（默认 5001 被占时用）
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict

# 修正 cwd 到项目根（与 web_only.py 同理：代码内有相对路径假设）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.chdir(PROJECT_ROOT)

import aiohttp

from adapt.onebot import OneBotChannel, OneBotChannelConfig
from config.adapt_settings import get_onebot_config


def _fake_event(kind: str) -> Dict[str, Any]:
    """最小 OneBot v11 事件（self_id 用测试号，避免触碰真实账号语义）。"""
    base: Dict[str, Any] = {
        "time": 0, "self_id": "10000",
    }
    if kind == "lifecycle":
        base.update({
            "post_type": "meta_event", "meta_event_type": "lifecycle",
            "sub_type": "connect",
        })
        return base
    base.update({
        "post_type": "message", "message_type": "group", "sub_type": "normal",
        "group_id": 123456, "user_id": 10001, "message_id": 999999,
        "message": [{"type": "text", "data": {"text": "onebot_check 冒烟测试"}}],
        "raw_message": "onebot_check 冒烟测试",
        "sender": {"user_id": 10001, "nickname": "smoke"},
    })
    return base


async def _connect_as_snowluma(url: str, token: str | None) -> tuple[bool, str]:
    """模拟 SnowLuma：query 参数与 Bearer header 同时携带。"""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    params = {"access_token": token} if token else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, headers=headers, params=params) as ws:
                await ws.send_json(_fake_event("lifecycle"))
                await ws.send_json(_fake_event("message"))
                await asyncio.sleep(0.3)
                return True, "连接被接受，lifecycle + message 事件已发送"
    except aiohttp.WSServerHandshakeError as e:
        return False, f"握手被拒: HTTP {e.status}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def _run(port_override: int | None) -> int:
    cfg = OneBotChannelConfig.from_platform_config(get_onebot_config())
    if port_override:
        cfg.port = port_override
    token = cfg.access_token
    url = f"ws://{cfg.host}:{cfg.port}{cfg.path}"

    channel = OneBotChannel(cfg)
    await channel.start()
    print(f"OneBotChannel 已启动: {url}")
    print(f"token: {'<未配置(不鉴权)>' if not token else '<已配置，不显示内容>'}")

    results = []
    ok, detail = await _connect_as_snowluma(url, token)
    results.append(("正确 token 连接", ok, detail))
    await asyncio.sleep(0.2)
    ok_bad, detail_bad = await _connect_as_snowluma(url, (token or "x") + "wrong")
    results.append(("错误 token 被拒绝", not ok_bad, detail_bad))
    await channel.stop()

    print("=" * 56)
    failed = False
    for name, passed, detail in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")
        failed = failed or not passed
    print("=" * 56)
    if not failed:
        print("Core 侧 OneBot 服务端就绪，可按 docs/setup_snowluma.md 配置 SnowLuma 连接。")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="OneBot 反向 WS 服务端自检")
    parser.add_argument("--port", type=int, default=None, help="临时覆盖端口")
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args.port))
    except OSError as e:
        if "10048" in str(e) or "10013" in str(e):
            print(f"\n[X] 端口被占用: {e}")
            print("    完整版 Core 可能正在运行（已占 5001）；换 --port 或先停 Core。")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
