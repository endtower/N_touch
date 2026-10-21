#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""adapt.web.system_api —— 系统管理接口（Core 侧代理）

职责边界（任务书 §3/§4）：本模块不做进程控制，所有启停/重启/关闭命令
转交给常驻管理服务（127.0.0.1:49990, manager.exe）。Core 未运行时管理
能力由管理服务与 WPF 启动器承担；本接口只在 Core 在线时为网页面板服务。

    GET  /system/status               Core 自身信息 + 服务端目标快照（代理）
    POST /system/snowluma/start|stop  → 服务 /v1/targets/snowluma/...
    POST /system/core/shutdown        → 服务 /v1/targets/core/stop（优雅优先）
    POST /system/core/restart         → 服务 /v1/targets/core/restart
    GET  /system/logs?name=&lines=    日志尾部（core/core_out/snowluma/service）
    GET/PUT /system/config            Core 部署配置白名单字段（代理到服务）

鉴权：WebConfig.ACCESS_TOKEN 非空时要求 ?access_token= 或 Bearer（与 /web/ws 一致）。
向管理服务的代理调用使用本地服务令牌（data_root/manager/service_token.txt）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import aiohttp
from aiohttp import web

from config.adapt_settings import get_onebot_config, get_web_config
from paths import data_root, log_root

_SERVICE_BASE = "http://127.0.0.1:49990"
_CORE_LOG_DIR = log_root()

# uvicorn.Server 句柄（main.py 生产模式注入）：内部直达关闭入口使用
_uvicorn_server: dict = {"server": None}


def set_uvicorn_server(server) -> None:
    _uvicorn_server["server"] = server


def _manager_token() -> str:
    try:
        return (data_root() / "manager" / "service_token.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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


def _service_token() -> str:
    try:
        return (data_root() / "manager" / "service_token.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def _service_call(method: str, path: str, body: dict | None = None,
                        timeout: float = 45) -> tuple[int | None, dict]:
    """异步调用管理服务 /v1 接口（不阻塞事件循环，复核 R1）。"""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            # aiohttp 的签名是 request(method, url, ...)：URL 必须走关键字传参，
            # 否则会被当作第一个位置参数 method，与 method=method 冲突（TypeError）。
            async with session.request(method=method, url=_SERVICE_BASE + path,
                                       headers={"X-Manager-Token": _service_token()},
                                       json=body) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    data = {"message": await resp.text()}
                return resp.status, data
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return None, {"error_code": "service_down",
                      "message": "管理服务未运行——进程管理请使用 AbyssLauncher 或 manager.py"}


async def _proxy(result: tuple[int | None, dict]) -> web.Response:
    status, body = result
    if status is None:
        return web.json_response(body, status=503)
    return web.json_response(body, status=status if status != 200 else 200)


# ============================================================
# 日志
# ============================================================

def _resolve_log_path(name: str) -> Path | None:
    today = date.today().isoformat()
    if name == "core":
        return _CORE_LOG_DIR / f"app_{today}.log"
    if name == "core_out":
        return _CORE_LOG_DIR / "manager" / "core.out.log"
    if name == "snowluma":
        return _CORE_LOG_DIR / "manager" / "snowluma.out.log"
    if name == "service":
        return _CORE_LOG_DIR / "manager" / "service.out.log"
    return None


def _tail_lines(path: Path, n: int, max_bytes: int = 256_000) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()[-n:]


async def handle_system_logs(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    name = request.query.get("name", "core")
    try:
        n = max(1, min(int(request.query.get("lines", "200")), 2000))
    except ValueError:
        n = 200
    path = _resolve_log_path(name)
    if path is None:
        return web.json_response({"error": f"未知日志名 {name}"}, status=404)
    return web.json_response({"name": name, "file": str(path), "lines": _tail_lines(path, n)})


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
            path = _resolve_log_path(name)   # core 主日志按天换文件名
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
            elif size < pos:
                pos = 0
    except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
        pass
    return ws


# ============================================================
# 状态（Core 本地 + 管理服务快照代理）
# ============================================================

async def handle_system_status(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    started_at = request.app.get("started_at")
    server = request.app.get("uvicorn_server")
    svc_status, svc_body = await _service_call("GET", "/v1/targets", timeout=3)
    snowluma = {"running": None, "pid": None,
                "note": "管理服务未运行，状态未知"}
    core_from_service = {}
    ingress = {"running": None, "ready": False, "onebot_connected": None}
    if svc_status == 200:
        for t in svc_body.get("targets", []):
            if t["name"] == "snowluma":
                webui_port = t["ports"][0] if t.get("ports") else None
                snowluma = {"running": t["state"] in ("running", "starting"),
                            "pid": t["pid"], "state": t["state"],
                            "ports": t["ports"],
                            "webui_url": f"http://127.0.0.1:{webui_port}/" if webui_port else ""}
            if t["name"] == "core":
                core_from_service = {"state": t["state"], "desired": t["desired"],
                                     "managed": t["managed"],
                                     "last_error": t["last_error"]}
            if t["name"] == "ingress":
                ingress = {**t, "running": t["state"] == "running"}
    return web.json_response({
        "core": {
            "running": True,
            "pid": os.getpid(),
            "uptime_sec": round(time.time() - started_at, 1) if started_at else None,
            "web_port": get_web_config().WS_PORT,
            "onebot_port": get_onebot_config().WS_PORT,
            "mode": "prod" if server is not None else "dev",
            "desired": core_from_service.get("desired", "running"),
            "state": core_from_service.get("state", "running"),
            "last_error": core_from_service.get("last_error", ""),
        },
        "snowluma": snowluma,
        "ingress": ingress,
        "service": {"reachable": svc_status == 200,
                    "config_errors": svc_body.get("config_errors", []) if svc_status == 200 else []},
        "hint": "Core 冷启动（进程已死）请用 AbyssLauncher / manager.py",
    })


# ============================================================
# 内部直达关闭入口（仅供常驻管理服务调用）
# ============================================================

async def handle_core_shutdown_internal(request: web.Request) -> web.Response:
    """让本 uvicorn 退出（完整 lifespan 收尾）。只做自身退出，不回调管理服务。

    鉴权：管理服务令牌（与 /v1 同源凭据）；仅监听 127.0.0.1。
    """
    if request.headers.get("X-Manager-Token", "") != _manager_token():
        return web.json_response({"error_code": "unauthorized"}, status=401)
    server = _uvicorn_server["server"]
    if server is None:
        return web.json_response({"error_code": "not_supported",
                                  "message": "dev(--reload) 模式无此入口"}, status=400)
    server.should_exit = True
    return web.json_response({"shutting_down": True})


# ============================================================
# 控制命令（全部转交管理服务）
# ============================================================

async def handle_snowluma_start(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return await _proxy(await _service_call("POST", "/v1/targets/snowluma/start"))


async def handle_snowluma_stop(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return await _proxy(await _service_call("POST", "/v1/targets/snowluma/stop"))


async def handle_core_shutdown(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return await _proxy(await _service_call("POST", "/v1/targets/core/stop"))


async def handle_core_restart(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return await _proxy(await _service_call("POST", "/v1/targets/core/restart"))


# ============================================================
# 配置代理（白名单字段；校验在管理服务侧）
# ============================================================

async def handle_config_get(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return await _proxy(await _service_call("GET", "/v1/config/core"))


async def handle_config_put(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad_json"}, status=400)
    return await _proxy(await _service_call("PUT", "/v1/config/core", body))


async def handle_connections(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return await _proxy(await _service_call("GET", "/v1/connections"))


async def handle_connection_check(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "bad_json"}, status=400)
    return await _proxy(await _service_call("POST", "/v1/connections/check", body))


async def handle_connections_apply(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return await _proxy(await _service_call("POST", "/v1/connections/apply"))
