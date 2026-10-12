#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manager_service —— Abyss 独立常驻管理服务（无界面入口，打包为 manager.exe）

唯一进程控制者：WPF 窗口、CLI、Core 网页系统面板都通过本服务的 /v1 接口
提交命令。不导入 SystemCore/业务代码，不初始化业务数据库/人格/LLM；
Core 配置非法时服务照常运行（降级端口 + 错误上报）。

接口（仅监听 127.0.0.1，需 X-Manager-Token 头）：
    GET  /v1/info                    服务身份/版本/数据目录
    GET  /v1/targets                 全部目标状态快照
    POST /v1/targets/{t}/start|stop|restart    串行化启停命令
    GET  /v1/logs/{source}?lines=    日志尾部（core/core_out/snowluma/service）
    POST /v1/shutdown                停止全部受管目标并退出服务
配置读写（R5 更新前的最小配置面）：
    GET/PUT /v1/config/core          .env.dev 白名单字段（端口/路径/token）

令牌：首次启动生成 data_root/manager/service_token.txt；WPF 与 CLI 从同一路径读取。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import sys
import time
from pathlib import Path

from paths import app_root, data_root, ensure_cwd, log_root

app_root()
ensure_cwd()
sys.path.insert(0, str(app_root()))

from aiohttp import web                                     # noqa: E402
from manager_core import SupervisorService, log_root        # noqa: E402

SERVICE_VERSION = "0.5.0-ingress"
API_PORT = 49990
_TOKEN_FILE = data_root() / "manager" / "service_token.txt"

_service: SupervisorService | None = None
_started_at = time.time()


# ============================================================
# 令牌与鉴权
# ============================================================

def _load_or_create_token() -> str:
    try:
        token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


TOKEN = _load_or_create_token()


def _authorized(request: web.Request) -> bool:
    return request.headers.get("X-Manager-Token", "") == TOKEN


def _denied() -> web.Response:
    return web.json_response({"error_code": "unauthorized", "message": "管理令牌缺失或不匹配"}, status=401)


def _target(request: web.Request):
    name = request.match_info.get("target", "")
    ctrl = _service.controller(name)
    return ctrl, (None if ctrl else f"未知目标 {name}（可选 ingress | core | snowluma）")


# ============================================================
# 路由
# ============================================================

async def handle_info(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return web.json_response({
        "service": "abyss-manager",
        "version": SERVICE_VERSION,
        "api": "v1",
        "instance": str(data_root()),
        "uptime_sec": round(time.time() - _started_at, 1),
        "config_errors": _service.config_errors,
    })


async def handle_targets(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    targets = [vars(s) for s in _service.snapshots()]
    for target in targets:
        if target["name"] == "ingress" and target["ready"]:
            from aiohttp import ClientSession, ClientTimeout
            try:
                base = _service.controller("ingress").spec.health_url.rsplit("/", 1)[0]
                async with ClientSession(timeout=ClientTimeout(total=1)) as client:
                    async with client.get(base + "/v1/status", headers={"X-Manager-Token": TOKEN}) as response:
                        if response.status == 200:
                            detail = await response.json()
                            target["ready"] = bool(detail.get("ready"))
                            target["onebot_connected"] = detail.get("onebot_connected")
                            target["pending"] = detail.get("pending", {})
                            if detail.get("last_error"):
                                target["last_error"] = detail["last_error"]
            except Exception:
                target["onebot_connected"] = None
                target["ready"] = False
    return web.json_response({
        "targets": targets,
        "config_errors": _service.config_errors,
        "service_started_at": _started_at,
    })


async def handle_target_cmd(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    ctrl, err = _target(request)
    if ctrl is None:
        return web.json_response({"error_code": "bad_target", "message": err}, status=404)
    kind = request.match_info["action"]
    # 控制器内部是线程同步等待（健康等待最长 30s+），转线程执行，
    # 保证服务事件循环在启停期间仍可响应状态/日志/停止请求（复核 R1）
    result = await asyncio.to_thread(ctrl.submit, kind)
    status = 200 if result.get("ok") else (409 if result.get("error_code") == "conflict" else 200)
    return web.json_response(result, status=status)


async def handle_logs(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    name = request.match_info.get("source", "core")
    try:
        n = max(1, min(int(request.query.get("lines", "200")), 2000))
    except ValueError:
        n = 200
    path = _log_path(name)
    if path is None:
        return web.json_response({"error_code": "bad_source",
                                  "message": "未知日志源（core | core_out | snowluma | service）"}, status=404)
    lines: list[str] = []
    if path.exists():
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256_000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()[-n:]
    return web.json_response({"name": name, "file": str(path), "lines": lines})


def _log_path(name: str) -> Path | None:
    today = time.strftime("%Y-%m-%d")
    if name == "core":
        return log_root() / f"app_{today}.log"
    if name == "core_out":
        return log_root() / "manager" / "core.out.log"
    if name == "ingress":
        return log_root() / "manager" / "ingress.out.log"
    if name == "snowluma":
        return log_root() / "manager" / "snowluma.out.log"
    if name == "service":
        return log_root() / "manager" / "service.out.log"
    return None


# ============================================================
# 配置读写（白名单字段，原子写入 .env.dev）
# ============================================================

_CONFIG_ALLOW = {
    "WEB_WS_PORT": int,
    "ONEBOT_WS_PORT": int,
    "ONEBOT_WS_PATH": str,
    "ONEBOT_ACCESS_TOKEN": str,
    "INGRESS_API_PORT": int,
}


def _env_path() -> Path:
    return data_root() / ".env.dev"


def _read_env_map() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in _env_path().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    except OSError:
        pass
    return result


async def handle_config_get(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    env = _read_env_map()
    fields = {}
    for key, cast in _CONFIG_ALLOW.items():
        raw = env.get(key, "")
        try:
            fields[key] = cast(raw) if raw else (0 if cast is int else "")
        except (TypeError, ValueError):
            fields[key] = raw
    # 掩码：token 只返回是否已设置，不回传明文
    if fields.get("ONEBOT_ACCESS_TOKEN"):
        fields["ONEBOT_ACCESS_TOKEN_SET"] = True
        fields.pop("ONEBOT_ACCESS_TOKEN", None)
    return web.json_response({"file": str(_env_path()), "fields": fields})


async def handle_config_put(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error_code": "bad_json", "message": "请求体不是 JSON"}, status=400)
    updates = body.get("fields", {})
    unknown = [k for k in updates if k not in _CONFIG_ALLOW]
    if unknown:
        return web.json_response({"error_code": "unknown_field",
                                  "message": f"不允许的字段: {', '.join(unknown)}"}, status=400)
    # 校验
    for key, value in updates.items():
        if _CONFIG_ALLOW[key] is int:
            try:
                if not (1 <= int(value) <= 65535):
                    raise ValueError
            except (TypeError, ValueError):
                return web.json_response({"error_code": "bad_value",
                                          "message": f"{key}={value!r} 不是合法端口"}, status=400)
        elif key == "ONEBOT_WS_PATH" and value and not value.startswith("/"):
            return web.json_response({"error_code": "bad_value",
                                      "message": "ONEBOT_WS_PATH 必须以 / 开头"}, status=400)
    # 原子写入：读原文件 → 替换/追加白名单键 → 写临时文件 → 替换
    env_path = _env_path()
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for key, value in updates.items():
        marker = f"{key}="
        idx = next((i for i, l in enumerate(lines) if l.strip().startswith(marker)), None)
        if idx is None:
            lines.append(f"{key}={value}")
        else:
            lines[idx] = f"{key}={value}"
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(env_path)
    return web.json_response({"saved": True, "file": str(env_path),
                              "note": "已保存，待应用——接入配置重启消息接入服务生效；Web 配置重启 Core 生效"})


async def handle_shutdown(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    msgs = await _service.stop_everything_async()
    # 受管目标已按优雅流程停止；服务自身无持久状态，延迟自退（给响应留出送达时间）
    asyncio.get_event_loop().call_later(1.0, lambda: os._exit(0))
    return web.json_response({"ok": True, "shutting_down": True, "stopped": msgs})


# ============================================================
# 入口
# ============================================================

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/info", handle_info)
    app.router.add_get("/v1/targets", handle_targets)
    app.router.add_post("/v1/targets/{target}/{action}", handle_target_cmd)
    app.router.add_get("/v1/logs/{source}", handle_logs)
    app.router.add_get("/v1/config/core", handle_config_get)
    app.router.add_put("/v1/config/core", handle_config_put)
    app.router.add_post("/v1/shutdown", handle_shutdown)
    from manager_console import register_console
    register_console(app, _service, TOKEN, config_handlers=(handle_config_get, handle_config_put))
    return app


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Abyss 管理服务（无界面常驻）")
    parser.add_argument("--port", type=int, default=API_PORT)
    args = parser.parse_args()

    # 无控制台启动时自行持有日志文件，窗口关闭不影响服务输出。
    if sys.stdout is None or not sys.stdout.isatty():
        output = log_root() / "manager" / "service.out.log"
        output.parent.mkdir(parents=True, exist_ok=True)
        stream = output.open("a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = stream
        sys.stderr = stream

    global _service, _started_at
    from aiohttp import web as _web
    _service = SupervisorService()
    _started_at = time.time()
    app = build_app()
    print(f"[manager-service] v{SERVICE_VERSION} listening on 127.0.0.1:{args.port} "
          f"(data={data_root()})", flush=True)
    _web.run_app(app, host="127.0.0.1", port=args.port, print=None,
                 handle_signals=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
