#!/usr/bin/env python3
"""Abyss 独立消息接入入口；不启动 AMY、Attention 或任何 agent。"""
import argparse
import asyncio
import sys

from paths import ensure_cwd, log_root


def main():
    ensure_cwd()
    if sys.stdout is None:
        path = log_root() / "manager" / "ingress.out.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = path.open("a", encoding="utf-8", errors="replace", buffering=1)
        sys.stderr = sys.stdout
    parser = argparse.ArgumentParser(description="Abyss 消息接入服务")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    from adapt.ingress_runtime import run_service
    try:
        asyncio.run(run_service(args.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
