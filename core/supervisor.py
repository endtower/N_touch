# -*- coding: utf-8 -*-
"""core.supervisor —— 进程监督共享模块

manager.py（CLI 管家）与 adapt/web/system_api.py（网页系统面板）共用的
进程控制原语：进程规格、端口/健康探测、拉起、按树硬停止。

设计约定：
- 子进程 stdout/stderr 重定向到 logs/manager/<name>.out.log（追加），不用管道——
  监督者退出不会让子进程因缓冲区满而卡死，任何一方都可以随时"脱离"；
- 从 Core 网页接口拉起 SnowLuma 时用 DETACHED_PROCESS：脱离 Core 的控制台，
  Core 重启不影响它（QQ 登录态比 Core 的命贵）；
- stop 一律 taskkill /T /F 硬停止（SQLite WAL 保证数据安全）；优雅关闭走
  Core 自己的 /system/core/shutdown。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from paths import app_root, data_root, log_root

PROJECT_ROOT = app_root()
LOG_DIR = log_root() / "manager"
SNOWLUMA_DIR = PROJECT_ROOT / "SnowLuma"

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

HEALTH_TIMEOUT_SEC = 30


def manager_cmd(*args: str) -> list:
    """管家入口命令：冻结运行用 manager.exe，源码运行用 python manager.py。"""
    if getattr(sys, "frozen", False):
        return [str(app_root() / "manager.exe"), *args]
    return [sys.executable, str(PROJECT_ROOT / "manager.py"), *args]


def core_cmd() -> list:
    """Core 启动命令：冻结运行用 core.exe，源码运行用 python -u main.py。"""
    if getattr(sys, "frozen", False):
        return [str(app_root() / "core.exe")]
    return [sys.executable, "-u", "main.py"]


def snowluma_webui_port() -> int:
    try:
        cfg = json.loads((SNOWLUMA_DIR / "config" / "runtime.json").read_text(encoding="utf-8"))
        return int(cfg.get("webuiPort", 5099))
    except Exception:
        return 5099


def core_ports() -> tuple[int, int]:
    """Core 的 (web, onebot) 端口——走 .env.dev 覆盖层，与真实启动一致。"""
    from config.adapt_settings import get_onebot_config, get_web_config
    return get_web_config().WS_PORT, get_onebot_config().WS_PORT


@dataclass
class ProcSpec:
    name: str
    cmd: list
    cwd: Path
    out_log: Path
    ports: list                    # 任一端口在监听即视为「活着」
    health_url: str = ""           # 可选 HTTP 健康检查（start 时等待它就绪）
    order: int = 0                 # start all 的顺序
    auto_restart: bool = True
    desc: str = ""


def build_specs() -> dict[str, ProcSpec]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    web_port, onebot_port = core_ports()
    return {
        "core": ProcSpec(
            name="core",
            cmd=core_cmd(),
            cwd=PROJECT_ROOT,
            out_log=LOG_DIR / "core.out.log",
            ports=[web_port, onebot_port],
            health_url=f"http://127.0.0.1:{web_port}/health",
            order=0,
            desc="Abyss Core（大脑 + Web + OneBot）",
        ),
        "snowluma": ProcSpec(
            name="snowluma",
            cmd=["cmd", "/c", "launcher.bat"],
            cwd=SNOWLUMA_DIR,
            out_log=LOG_DIR / "snowluma.out.log",
            ports=[snowluma_webui_port(), 3001],
            order=1,
            auto_restart=False,   # QQ 登录态宝贵，崩溃自动重启可能丢登录
            desc=f"SnowLuma（QQ 登录，WebUI :{snowluma_webui_port()}）",
        ),
    }


# ============================================================
# 探测
# ============================================================

def port_alive(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def spec_alive(spec: ProcSpec) -> bool:
    return any(port_alive(p) for p in spec.ports)


def pid_listening_on(port: int) -> int | None:
    """netstat 反查占用端口的 PID。netstat 输出是本地编码（GBK），按字节宽松解码。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=10).stdout
        for line in out.decode("utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3] == "LISTENING" and f":{port}" in parts[1]:
                return int(parts[4])
    except Exception:
        pass
    return None


def health_ok(spec: ProcSpec) -> bool:
    if not spec.health_url:
        return spec_alive(spec)
    try:
        with urllib.request.urlopen(spec.health_url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def wait_ports_free(spec: ProcSpec, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not spec_alive(spec):
            return True
        time.sleep(0.4)
    return not spec_alive(spec)


# ============================================================
# 期望状态标记（GPT 审查-2：三端统一协调点）
# ============================================================
# logs/manager/desire/<name>.txt = "running" | "stopped" | "restart"
# - 网页 /system/core/shutdown → 写 "stopped" 再优雅退出：看门狗读到后不再拉起
# - 网页 /system/core/restart  → 写 "restart" 再优雅退出：看门狗负责拉回
# - 托盘/CLI 的 start/stop 经 Manager 同步写标记，三端语义一致
# - 意外崩溃时标记仍为 "running" → 看门狗按退避策略自动恢复

def desire_path(name: str) -> Path:
    return LOG_DIR / "desire" / f"{name}.txt"


def read_desire(name: str, default: str = "stopped") -> str:
    try:
        value = desire_path(name).read_text(encoding="utf-8").strip()
        return value if value in ("running", "stopped", "restart") else default
    except OSError:
        return default


def write_desire(name: str, value: str) -> None:
    path = desire_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


# ============================================================
# 启停
# ============================================================

def spawn(spec: ProcSpec, detached: bool = False, clean_env: bool = False) -> subprocess.Popen:
    """拉起进程（输出追加到 spec.out_log），等待健康就绪由调用方决定。

    detached=True: DETACHED_PROCESS——完全脱离当前控制台与生命周期。
    clean_env=True: 从继承环境剔除配置覆盖键（WEB_WS_PORT 等），让子进程
    从 .env.dev 重新读取最新值——避免常驻服务的旧环境覆盖用户新保存的
    配置（复核 R4-4）。
    """
    spec.out_log.parent.mkdir(parents=True, exist_ok=True)
    out = open(spec.out_log, "ab")
    flags = CREATE_NEW_PROCESS_GROUP | (DETACHED_PROCESS if detached else 0)
    env = None
    if clean_env:
        from config.env_tools import CONFIG_OVERRIDE_KEYS
        env = os.environ.copy()
        for key in CONFIG_OVERRIDE_KEYS:
            env.pop(key, None)
    try:
        return subprocess.Popen(
            spec.cmd, cwd=spec.cwd,
            stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=flags, env=env,
        )
    finally:
        out.close()   # 子进程已继承句柄，父进程即可关闭


def stop_tree(spec: ProcSpec, pid: int | None) -> tuple[bool, int | None]:
    """taskkill /T /F 硬停止进程树，并等待端口释放。

    pid 传 None 时自动按端口反查（接管外部实例）。返回 (是否已停, 实际杀的 pid)。
    """
    if pid is None:
        pid = pid_listening_on(spec.ports[0])
    if pid is None:
        return True, None
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    freed = wait_ports_free(spec, timeout=10)
    return freed, pid
