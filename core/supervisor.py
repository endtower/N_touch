# -*- coding: utf-8 -*-
"""手动管理服务的进程原语：规格、探测、拉起和按已知 PID 停止。

子进程输出写入 logs/manager。这里只操作管理服务明确持有的进程，
不按端口接管外部程序，不扫描安装目录清扫，不保存恢复状态。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from paths import app_root, data_root, log_root

PROJECT_ROOT = app_root()
LOG_DIR = log_root() / "manager"
SNOWLUMA_DIR = (data_root() / "SnowLuma") if (data_root() / "SnowLuma").is_dir() else PROJECT_ROOT / "SnowLuma"

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

HEALTH_TIMEOUT_SEC = 30


def python_exe() -> str:
    """后端源码运行的 Python 解释器。

    优先级：ABYSS_PYTHON 环境变量 → 数据根 .venv → PATH 上的 python / py。
    解析不到直接抛错：后端无法以源码启动时必须阻断，不用错解释器硬起。
    """
    override = os.getenv("ABYSS_PYTHON", "").strip()
    if override:
        return override
    venv = data_root() / ".venv" / "Scripts" / "python.exe"
    if venv.is_file():
        return str(venv)
    for name in ("python", "py"):
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(
        "未找到可用的 Python 解释器：请安装 Python，或设置 ABYSS_PYTHON "
        "指向解释器，或在数据根放置 .venv"
    )


def source_root() -> Path:
    """后端源码根（main.py / ingress_service.py 所在）。

    冻结的管理服务取 data_root（abyss-data-dir.txt 指向仓库根），源码运行取
    app_root；以 main.py 实际存在为准，二者都没有直接抛错。
    """
    for root in (data_root(), app_root()):
        if (root / "main.py").is_file():
            return root
    raise FileNotFoundError(
        f"未找到后端源码根（main.py 所在目录）：{data_root()} / {app_root()}"
    )


def core_cmd() -> list:
    """Core 启动命令：始终以源码方式拉起（改完点重启即生效，不随 exe 冻结）。"""
    return [python_exe(), "-u", str(source_root() / "main.py")]


def ingress_cmd() -> list:
    """Ingress 启动命令：同样始终以源码方式拉起。"""
    return [python_exe(), "-u", str(source_root() / "ingress_service.py")]


def snowluma_webui_port() -> int:
    try:
        cfg = json.loads((SNOWLUMA_DIR / "config" / "runtime.json").read_text(encoding="utf-8"))
        return int(cfg.get("webuiPort", 5099))
    except Exception:
        return 5099


def core_ports() -> tuple[int, int]:
    """Core 的 (web, onebot) 端口——走 .env.dev 覆盖层，与真实启动一致。"""
    from dotenv import dotenv_values
    env = dotenv_values(data_root() / ".env.dev")
    ports = tuple(int(os.environ.get(key) or env.get(key) or default)
                  for key, default in (("WEB_WS_PORT", 5002), ("ONEBOT_WS_PORT", 5001)))
    if any(not 1 <= port <= 65535 for port in ports):
        raise ValueError("Core 端口必须在 1–65535 范围内")
    return ports


@dataclass
class ProcSpec:
    name: str
    cmd: list
    cwd: Path
    out_log: Path
    ports: list                    # 任一端口在监听即视为「活着」
    health_url: str = ""           # 可选 HTTP 健康检查（start 时等待它就绪）
    order: int = 0                 # start all 的顺序
    desc: str = ""
    dependencies: tuple[str, ...] = ()


def build_specs() -> dict[str, ProcSpec]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    web_port, onebot_port = core_ports()
    from ingress_config import api_port, onebot_enabled
    ingress_port = api_port()
    if ingress_port in (web_port, onebot_port) or web_port == onebot_port:
        raise ValueError("Web、OneBot、接入 API 端口必须互不相同")
    return {
        "ingress": ProcSpec(
            name="ingress", cmd=ingress_cmd(), cwd=PROJECT_ROOT,
            out_log=LOG_DIR / "ingress.out.log",
            ports=[ingress_port] + ([onebot_port] if onebot_enabled() else []),
            health_url=f"http://127.0.0.1:{ingress_port}/health",
            order=0, desc="消息接入（SnowLuma 连接 + 消息落库）",
        ),
        "core": ProcSpec(
            name="core",
            cmd=core_cmd(),
            cwd=PROJECT_ROOT,
            out_log=LOG_DIR / "core.out.log",
            ports=[web_port],
            health_url=f"http://127.0.0.1:{web_port}/health",
            order=2,
            desc="Abyss Core（大脑 + Web；消息接入独立运行）",
            dependencies=("ingress",),
        ),
        "snowluma": ProcSpec(
            name="snowluma",
            cmd=["cmd", "/c", "launcher.bat"],
            cwd=SNOWLUMA_DIR,
            out_log=LOG_DIR / "snowluma.out.log",
            ports=[snowluma_webui_port(), 3001],
            order=1,
            desc=f"SnowLuma（QQ 登录，WebUI :{snowluma_webui_port()}）",
            dependencies=("ingress",),
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
    # CREATE_NO_WINDOW：管理服务自身无控制台时，Windows 会给控制台子进程
    # 新开可见终端——加此标志让 Core/SnowLuma 静默后台运行（日志已在文件里）
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | (DETACHED_PROCESS if detached else 0)
    env = None
    if clean_env:
        from config_keys import CONFIG_OVERRIDE_KEYS
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

    没有受管 PID 时拒绝终止；不按端口推断归属。返回 (端口是否释放, PID)。
    """
    if pid is None:
        return False, None
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    freed = wait_ports_free(spec, timeout=10)
    return freed, pid
