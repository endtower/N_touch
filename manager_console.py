"""常驻管理页面：文件查询/启动配置独立运行，业务流量只转发到已就绪 core。"""

from __future__ import annotations

import asyncio
import secrets
import time
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from console_context import list_chains, read_chain_file
from console_history import list_sessions, read_messages
from paths import data_root, log_root, web_dist
from truck_settings_store import snapshot, update_settings

COOKIE = "abyss_console_session"
SESSION_TTL = 12 * 3600
PROXY_PREFIXES = ("/web/", "/media/", "/llm-save/", "/stickers/", "/graph/",
                  "/events", "/amy/", "/notes", "/memory/", "/token-stats")


class Console:
    def __init__(self, service, manager_token):
        self.service = service
        self.manager_token = manager_token
        self.sessions = {}
        self.client = None

    def authorized(self, request):
        header = request.headers.get("X-Manager-Token", "")
        if header and secrets.compare_digest(header, self.manager_token):
            return True
        return self.sessions.get(request.cookies.get(COOKIE, ""), 0) > time.time()

    def issue_session(self):
        now = time.time()
        self.sessions = {key: expiry for key, expiry in self.sessions.items() if expiry > now}
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + SESSION_TTL
        return token

    def core(self):
        ctrl = self.service.controller("core")
        return ctrl.snapshot() if ctrl else None

    @staticmethod
    def is_core_ready(core):
        return core is not None and core.state == "running" and core.ready

    def core_ready(self):
        return self.is_core_ready(self.core())

    def core_base(self, core=None):
        core = core if core is not None else self.core()
        return f"http://127.0.0.1:{core.ports[0]}"

    @staticmethod
    def core_unready(core):
        state = core.state if core else "stopped"
        pid = core.pid if core else None
        if state == "starting":
            code, message = "core_starting", "Core 正在启动，请稍后再试"
        elif state == "stopping":
            code, message = "core_stopping", "Core 正在停止，此操作暂不可用"
        elif pid is not None:
            code, message = "core_not_ready", "Core 进程仍在运行，但服务尚未就绪，请查看状态或稍后再试"
        elif state == "conflict":
            code, message = "core_conflict", "Core 端口被其他进程占用，请查看管理状态"
        else:
            code, message = "core_stopped", "Core 未运行，此操作需要启动 Core"
        return web.json_response({"error": message, "error_code": code,
                                  "core_state": state, "core_pid": pid}, status=503,
                                 headers={"Cache-Control": "no-store"})

    def upstream_headers(self):
        import os
        from dotenv import dotenv_values
        token = os.environ.get("WEB_ACCESS_TOKEN") or dotenv_values(data_root() / ".env.dev").get("WEB_ACCESS_TOKEN")
        headers = {"X-Manager-Token": self.manager_token}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def start(self, app):
        self.client = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=65))

    async def stop(self, app):
        await self.client.close()

    async def session(self, request):
        # 原生宿主用管理令牌兑换仅限管理页面的会话；令牌不会进入网页脚本。
        if not secrets.compare_digest(request.headers.get("X-Manager-Token", ""), self.manager_token):
            return web.json_response({"error": "管理令牌无效"}, status=401)
        token = self.issue_session()
        response = web.json_response({"session": token, "max_age": SESSION_TTL})
        response.set_cookie(COOKIE, token, httponly=True, samesite="Strict", max_age=SESSION_TTL)
        response.headers["Cache-Control"] = "no-store"
        return response

    async def status(self, request):
        core = self.core()
        return web.json_response({"manager_available": True, "core_ready": self.is_core_ready(core),
                                  "core": vars(core) if core else None},
                                 headers={"Cache-Control": "no-store"})

    async def trucks(self, request):
        if request.method == "PUT":
            try:
                await asyncio.to_thread(update_settings, await request.json())
            except (ValueError, TypeError) as e:
                return web.json_response({"error": str(e)}, status=400)
        if self.core_ready():
            response = await self.proxy(request, "/console/trucks", method="GET")
            if response.status < 400 or request.method != "PUT":
                return response
            # 保存已经成功，即便运行中的 Core 此刻断开，也准确确认持久化结果。
            data = await asyncio.to_thread(snapshot)
            for truck in data["trucks"]:
                truck["runtime_state"] = "unknown"
            data["core_ready"] = False
            return web.json_response(data)
        try:
            data = await asyncio.to_thread(snapshot)
            data["core_ready"] = False
            return web.json_response(data)
        except (ValueError, OSError) as e:
            return web.json_response({"error": f"Truck 配置读取失败: {e}"}, status=500)

    async def truck_action(self, request):
        return await self.proxy(request, "/console/trucks/" + request.match_info["action"])

    async def core_action(self, request):
        result = await asyncio.to_thread(self.service.controller("core").submit, request.match_info["action"])
        return web.json_response(result, status=200 if result.get("ok") else 409)

    async def system_status(self, request):
        if self.core_ready():
            return await self.proxy(request)
        core = self.core()
        snow = self.service.controller("snowluma")
        snow = snow.snapshot() if snow else None
        ingress_ctrl = self.service.controller("ingress")
        ingress = ingress_ctrl.snapshot() if ingress_ctrl else None
        return web.json_response({
            "core": {"running": False, "pid": core.pid if core else None,
                     "uptime_sec": core.uptime_sec if core else None,
                     "web_port": core.ports[0] if core else 5002,
                     "onebot_port": core.ports[1] if core and len(core.ports) > 1 else 5001,
                     "mode": "未运行", "graceful_shutdown": True},
            "snowluma": {"running": bool(snow and snow.state == "running"),
                         "pid": snow.pid if snow else None, "ports": snow.ports if snow else [],
                         "webui_url": f"http://127.0.0.1:{snow.ports[0] if snow else 5099}/"},
            "ingress": {**(vars(ingress) if ingress else {}),
                        "running": bool(ingress and ingress.state == "running")},
            "hint": "管理页面独立运行，可以查看记录和保存启动配置。",
        })

    async def system_action(self, request):
        target = "snowluma" if "/snowluma/" in request.path else "core"
        action = request.path.rsplit("/", 1)[-1]
        if action == "shutdown":
            action = "stop"
        result = await asyncio.to_thread(self.service.controller(target).submit, action)
        return web.json_response({**result, "started": bool(result.get("ok") and action == "start"),
                                  "stopped": bool(result.get("ok") and action == "stop"),
                                  "error": result.get("message") if not result.get("ok") else None},
                                 status=200 if result.get("ok") else 409)

    def log_lines(self, name, count):
        paths = {"core": log_root() / f"app_{time.strftime('%Y-%m-%d')}.log",
                 "ingress": log_root() / "manager/ingress.out.log",
                 "core_out": log_root() / "manager/core.out.log",
                 "snowluma": log_root() / "manager/snowluma.out.log",
                 "service": log_root() / "manager/service.out.log"}
        if name not in paths:
            raise ValueError("未知日志源")
        path = paths[name]
        if not path.exists():
            return []
        with path.open("rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 256000))
            return f.read().decode("utf-8", errors="replace").splitlines()[-count:]

    async def logs(self, request):
        name = request.query.get("name", "core")
        count = max(1, min(int(request.query.get("lines", "200")), 2000))
        return web.json_response({"name": name, "lines": await asyncio.to_thread(self.log_lines, name, count)})

    async def logs_ws(self, request):
        name = request.query.get("name", "core")
        count = max(1, min(int(request.query.get("lines", "200")), 2000))
        lines = await asyncio.to_thread(self.log_lines, name, count)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        try:
            while not ws.closed:
                await ws.send_json({"type": "snapshot", "lines": lines})
                # 消费 close 帧，客户端关闭时立即退出；不等待下一次日志变化。
                try:
                    message = await asyncio.wait_for(ws.receive(), timeout=1)
                    if message.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                except asyncio.TimeoutError:
                    pass
                lines = await asyncio.to_thread(self.log_lines, name, count)
        finally:
            await ws.close()
        return ws

    async def context(self, request):
        if self.core_ready():
            return await self.proxy(request)
        try:
            if request.path == "/context/chains":
                data = await asyncio.to_thread(list_chains)
                data["fetched_at"] = time.time()
                data["live"] = False
            else:
                data = await asyncio.to_thread(read_chain_file, request.query.get("chain", ""), request.query.get("name", ""))
            return web.json_response(data)
        except (ValueError, FileNotFoundError, PermissionError) as e:
            status = 404 if isinstance(e, FileNotFoundError) else 403 if isinstance(e, PermissionError) else 400
            return web.json_response({"error": str(e)}, status=status)

    async def history(self, request):
        if self.core_ready():
            return await self.proxy(request)
        try:
            if request.path == "/sessions":
                data = {"sessions": await asyncio.to_thread(list_sessions)}
            else:
                data = await asyncio.to_thread(read_messages, request.query.get("scope", ""),
                                               request.query.get("id", ""), int(request.query.get("limit", "50")))
            return web.json_response(data)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    async def media(self, request):
        if self.core_ready():
            return await self.proxy(request)
        folder = "memory/llm_save" if request.path.startswith("/llm-save/") else "memory/chat_history/media"
        root = (data_root() / folder).resolve()
        target = (root / request.match_info["tail"]).resolve()
        if not target.is_relative_to(root):
            raise web.HTTPForbidden()
        if not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target)

    async def proxy(self, request, path=None, method=None):
        core = self.core()
        if not self.is_core_ready(core):
            return self.core_unready(core)
        path = path or request.path
        headers = self.upstream_headers()
        if request.content_type:
            headers["Content-Type"] = request.headers.get("Content-Type", "application/json")
        try:
            async with self.client.request(method or request.method, self.core_base(core) + path,
                                           params=request.query, headers=headers,
                                           data=await request.read() if method != "GET" else None,
                                           allow_redirects=False) as response:
                return web.Response(status=response.status, body=await response.read(),
                                    headers={key: value for key, value in response.headers.items()
                                             if key.lower() in ("content-type", "cache-control", "content-disposition")})
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.json_response({"error": "Core 暂时无法连接", "error_code": "core_unavailable"}, status=503)

    async def websocket(self, request):
        core = self.core()
        if not self.is_core_ready(core):
            return self.core_unready(core)
        try:
            upstream = await self.client.ws_connect(self.core_base(core) + request.path,
                                                    params=request.query,
                                                    headers=self.upstream_headers(), heartbeat=30,
                                                    max_msg_size=100 * 1024 * 1024)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return web.json_response({"error": "Core 暂时无法连接"}, status=503)
        downstream = web.WebSocketResponse(heartbeat=30, max_msg_size=100 * 1024 * 1024)
        tasks = []
        try:
            await downstream.prepare(request)

            async def relay(source, target):
                async for message in source:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await target.send_str(message.data)
                    elif message.type == aiohttp.WSMsgType.BINARY:
                        await target.send_bytes(message.data)
                    else:
                        break

            tasks = [asyncio.create_task(relay(upstream, downstream)),
                     asyncio.create_task(relay(downstream, upstream))]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await upstream.close()
            await downstream.close()
        return downstream

    async def page(self, request):
        if not self.authorized(request):
            # 独立浏览器入口；桌面宿主自动设置 HttpOnly 会话 Cookie。
            return web.Response(text=LOGIN, content_type="text/html", headers={"Cache-Control": "no-store"})
        if request.path.startswith(("/console/", "/context/")):
            raise web.HTTPNotFound()
        root = web_dist().resolve()
        relative = request.match_info.get("tail", "") or "index.html"
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise web.HTTPForbidden()
        if not path.is_file():
            if relative.startswith("assets/"):
                raise web.HTTPNotFound()
            path = root / "index.html"
        if not path.is_file():
            return web.Response(text="前端尚未构建，请先构建 web_frontend。", status=503)
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"} if path.name == "index.html" else {})

    async def dispatch(self, request):
        if request.path.startswith(PROXY_PREFIXES):
            return await self.proxy(request)
        raise web.HTTPNotFound()


LOGIN = '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<title>Abyss 管理页面</title><style>body{font:16px system-ui;background:#16191e;color:#eee;max-width:480px;margin:12vh auto;padding:24px}input,button{padding:12px;margin-top:16px}input{width:90%}</style>
<h1>Abyss 管理页面</h1><p>桌面启动器会自动连接。独立浏览器请填写本机 manager/service_token.txt 中的管理令牌。</p>
<form><input type="password" autocomplete="off" placeholder="本机管理令牌" required><button>连接</button></form><p id="error"></p>
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();const input=document.querySelector('input');const r=await fetch('/console/session',{method:'POST',headers:{'X-Manager-Token':input.value}});input.value='';if(r.ok)location.reload();else document.querySelector('#error').textContent='连接失败，请检查令牌。'}</script></html>'''


def register_console(app, service, manager_token, config_handlers=None, connection_handlers=None):
    console = Console(service, manager_token)

    @web.middleware
    async def authorize(request, handler):
        if request.path.startswith("/v1/"):
            return await handler(request)
        if request.host.split(":", 1)[0] not in ("127.0.0.1", "localhost"):
            raise web.HTTPForbidden()
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost"):
            raise web.HTTPForbidden()
        if request.path == "/console/session":
            return await handler(request)
        is_api = request.path.startswith(("/console/", "/context/", "/system/", *PROXY_PREFIXES)) or request.path in ("/sessions", "/messages")
        if is_api and not console.authorized(request):
            return web.json_response({"error": "管理页面未登录"}, status=401)
        try:
            return await handler(request)
        except (OSError, ValueError) as e:
            return web.json_response({"error": str(e)}, status=500)

    app.middlewares.append(authorize)
    app.on_startup.append(console.start)
    app.on_cleanup.append(console.stop)
    app.router.add_post("/console/session", console.session)
    app.router.add_get("/v1/console-session", console.session)
    app.router.add_get("/console/status", console.status)
    app.router.add_get("/console/trucks", console.trucks)
    app.router.add_put("/console/trucks", console.trucks)
    app.router.add_post("/console/trucks/{action:apply|stop}", console.truck_action)
    app.router.add_post("/console/core/{action:start|stop|restart}", console.core_action)
    app.router.add_get("/context/chains", console.context)
    app.router.add_get("/context/file", console.context)
    app.router.add_get("/sessions", console.history)
    app.router.add_get("/messages", console.history)
    app.router.add_get("/media/{tail:.*}", console.media)
    app.router.add_get("/llm-save/{tail:.*}", console.media)
    app.router.add_get("/web/ws", console.websocket)
    app.router.add_get("/system/ws/logs", console.logs_ws)
    app.router.add_get("/system/status", console.system_status)
    app.router.add_get("/system/logs", console.logs)
    if config_handlers:
        async def config(request):
            handler = config_handlers[0 if request.method == "GET" else 1]
            return await handler(request.clone(headers={**request.headers, "X-Manager-Token": manager_token}))
        app.router.add_get("/system/config", config)
        app.router.add_put("/system/config", config)
    if connection_handlers:
        async def connection(request):
            handler = connection_handlers[request.path]
            return await handler(request.clone(headers={**request.headers, "X-Manager-Token": manager_token}))
        app.router.add_get("/system/connections", connection)
        app.router.add_post("/system/connections/check", connection)
        app.router.add_post("/system/connections/apply", connection)
    for path in ("/system/core/shutdown", "/system/core/restart", "/system/snowluma/start", "/system/snowluma/stop"):
        app.router.add_post(path, console.system_action)
    app.router.add_route("*", "/system/{tail:.*}", console.proxy)
    for prefix in PROXY_PREFIXES:
        app.router.add_route("*", prefix.rstrip("/"), console.proxy)
        app.router.add_route("*", prefix.rstrip("/") + "/{tail:.*}", console.proxy)
    app.router.add_get("/", console.page)
    app.router.add_get("/{tail:.*}", console.page)
    return console
