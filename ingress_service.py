#!/usr/bin/env python3
"""Abyss 独立消息接入入口；不启动 AMY、Attention 或任何 agent。"""
import argparse
import asyncio
import os
import sys

from paths import ensure_cwd, log_root


def main():
    ensure_cwd()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # 总日志 app_*.log 只由 core 进程写入：两个进程同写同一文件时，
    # Windows 下轮转所需的 rename 会因文件被占用而失败。此处兜底设置，
    # 冻结运行由管理服务通过 spawn 注入同一开关（见 core/supervisor.spawn）。
    os.environ.setdefault("LOG_GLOBAL_FILE", "0")
    if sys.stdout is None:
        from utils.logger import rotate_log_file

        path = log_root() / "manager" / "ingress.out.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        rotate_log_file(path)   # 重启先翻页，避免历史输出无限累积
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
