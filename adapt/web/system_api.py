# -*- coding: utf-8 -*-
"""
adapt.web.system_api —— 系统管理接口（管家增量②）

给前端「系统面板」用的进程管理与日志接口：
    GET  /system/status            Core 自身状态 + SnowLuma 状态
    POST /system/snowluma/start    拉起 SnowLuma（DETACHED，脱离 Core 生命周期）
    POST /system/snowluma/stop     按树硬停 SnowLuma
    POST /system/core/shutdown     Core 优雅关闭（uvicorn should_exit，走完整 lifespan 收尾）
    POST /system/core/restart      起一个脱离的 respawn 工人等端口释放后拉起新 Core，然后优雅关闭自己
    GET  /system/logs?name=&lines= 日志文件尾部（name ∈ core | core_out | snowluma）
    GET  /system/ws/logs?name=     WebSocket 实时日志流（snapshot + append 帧）

权限：WebConfig.ACCESS_TOKEN 非空时，所有 /system/* 要求
?access_token= 或 Authorization: Bearer（与 /web/ws 一致）。默认 None 不鉴权。

边界：Core 冷启动（进程都没了）无法由网页完成——用 manager.py 或托盘。
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import date
from pathlib import Path

from aiohttp import web

from config.adapt_settings import get_web_config
from paths import log_root
from core.supervisor import (
    LOG_DIR,
    PROJECT_ROOT,
    build_specs,
    pid_listening_on,
    read_desire,
    spawn,
    spec_alive,
    stop_tree,
    wait_ports_free,
    write_desire,
    snowluma_webui_port,
)

_specs = build_specs()
_CORE_LOG_DIR = log_root()

# uvicorn.Server 句柄：main.py（生产模式）启动时注入，供优雅关闭/自重启。
# dev(--reload) 模式不注入 → 相关接口返回 400。
_uvicorn_server: dict = {"server": None}


def set_uvicorn_server(server) -> None:
    _uvicorn_server["server"] = server


# ============================================================
# 鉴权与工具
# ============================================================

def _authorized(request: web.Request) -> bool:
    token = get_web_config().ACCESS_TOKEN
    if not token:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == token:
        return True
    return request.query.get("access_token") == token


def _denied() -> web.Response:
    return web.json_response({"error": "unauthorized"}, status=401)


def _resolve_log_path(name: str) -> Path | None:
    if name == "core":
        return _CORE_LOG_DIR / f"app_{date.today().isoformat()}.log"
    if name == "core_out":
        return LOG_DIR / "core.out.log"
    if name == "snowluma":
        return LOG_DIR / "snowluma.out.log"
    return None


def _tail_lines(path: Path, n: int, max_bytes: int = 256_000) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]


def _json_error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


# ============================================================
# 状态
# ============================================================

async def handle_system_status(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    web_port, onebot_port = _specs["core"].ports
    started_at = request.app.get("started_at")
    server = _uvicorn_server["server"]
    s_spec = _specs["snowluma"]
    s_alive = spec_alive(s_spec)
    return web.json_response({
        "core": {
            "running": True,
            "pid": os.getpid(),
            "uptime_sec": round(time.time() - started_at, 1) if started_at else None,
            "web_port": web_port,
            "onebot_port": onebot_port,
            "mode": "prod" if server is not None else "dev",
            "graceful_shutdown": server is not None,
            "desired": read_desire("core"),
        },
        "snowluma": {
            "running": s_alive,
            "pid": pid_listening_on(s_spec.ports[0]) if s_alive else None,
            "ports": s_spec.ports,
            "webui_url": f"http://127.0.0.1:{snowluma_webui_port()}",
        },
        "hint": "Core 冷启动（进程已死）请用 manager.py 或托盘，网页拉不起已死的宿主",
    })


# ============================================================
# SnowLuma 管理
# ============================================================

async def handle_snowluma_start(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    spec = _specs["snowluma"]
    if spec_alive(spec):
        pid = pid_listening_on(spec.ports[0])
        return web.json_response({"started": False, "reason": "已在运行", "pid": pid})
    proc = spawn(spec, detached=True)
    deadline = time.time() + 20
    while time.time() < deadline and not spec_alive(spec):
        await asyncio.sleep(0.4)
    ok = spec_alive(spec)
    return web.json_response({
        "started": ok,
        "pid": proc.pid,
        "webui_url": f"http://127.0.0.1:{snowluma_webui_port()}",
        "note": "" if ok else "进程已拉起但端口未就绪（可能正在登录 QQ），稍后刷新状态",
    })


async def handle_snowluma_stop(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    spec = _specs["snowluma"]
    if not spec_alive(spec):
        return web.json_response({"stopped": False, "reason": "未在运行"})
    freed, pid = stop_tree(spec, None)
    return web.json_response({"stopped": freed, "pid": pid})


# ============================================================
# Core 优雅关闭 / 自重启
# ============================================================

async def handle_core_shutdown(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    server = _uvicorn_server["server"]
    if server is None:
        return _json_error("dev(--reload) 模式不支持远程关闭，请在终端 Ctrl+C", 400)
    # 期望状态标记：常驻管家读到 "stopped" 后不会把这次关闭当崩溃拉起。
    # 没有管家在跑时该标记只是无害的落盘记录。
    write_desire("core", "stopped")
    server.should_exit = True
    return web.json_response({"shutting_down": True,
                              "note": "uvicorn 将走完整 lifespan 收尾（SystemCore 优雅退出）；"
                                      "重新启动请用 manager.exe / 托盘"})


async def handle_core_restart(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    server = _uvicorn_server["server"]
    if server is None:
        return _json_error("dev(--reload) 模式不支持远程重启，请在终端 Ctrl+C 后重启", 400)
    if not spec_alive(_specs["core"]):
        return _json_error("Core 未在运行", 409)
    # 期望状态标记（GPT 审查-2）：常驻管家的看门狗读到 "restart" 会在本进程
    # 优雅退出后立即拉起新实例。Core 不再自己 spawn 第二个管理进程。
    write_desire("core", "restart")
    server.should_exit = True
    return web.json_response({"restarting": True,
                              "note": "Core 正在优雅关闭，管家将自动拉起新实例（约 5~15s）"})


# ============================================================
# 日志：REST 尾部 + WS 实时流
# ============================================================

async def handle_system_logs(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    name = request.query.get("name", "core")
    try:
        n = max(1, min(int(request.query.get("lines", "200")), 2000))
    except ValueError:
        return _json_error("lines 不是整数")
    path = _resolve_log_path(name)
    if path is None:
        return _json_error(f"未知日志名 {name}（可选 core | core_out | snowluma）")
    return web.json_response({"name": name, "file": str(path),
                              "lines": _tail_lines(path, n)})


async def handle_logs_ws(request: web.Request) -> web.WebSocketResponse:
    if not _authorized(request):
        return web.Response(status=401, text="unauthorized")
    name = request.query.get("name", "core")
    try:
        n = max(1, min(int(request.query.get("lines", "200")), 2000))
    except ValueError:
        n = 200
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)

    path = _resolve_log_path(name)
    if path is None:
        await ws.send_json({"type": "error", "message": f"未知日志名 {name}"})
        return ws
    await ws.send_json({"type": "snapshot", "name": name,
                        "lines": _tail_lines(path, n) if path.exists() else [],
                        "missing": not path.exists()})
    pos = path.stat().st_size if path.exists() else 0

    try:
        while True:
            await asyncio.sleep(0.5)
            path = _resolve_log_path(name)   # core 日志按天换文件名
            if path is None or not path.exists():
                continue
            size = path.stat().st_size
            if size > pos:
                with open(path, "rb") as f:
                    f.seek(pos)
                    chunk = f.read(size - pos)
                pos = size
                await ws.send_json({"type": "append",
                                    "text": chunk.decode("utf-8", errors="replace")})
            elif size < pos:   # 轮转/清空
                pos = 0
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    return ws
