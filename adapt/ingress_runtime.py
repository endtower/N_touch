"""独立消息接入运行时：原始消息先提交，解析和媒体处理与接收循环分离。"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict

import aiosqlite
from aiohttp import web

from adapt.onebot.channel import OneBotChannel
from adapt.onebot.parser import parse_onebot_event
from adapt.bridge.config import load_bridge_config
from adapt.bridge.runtime import BridgeRuntime
from adapt.protocol import MessageSegment, OutboundPayload, OutboundTarget, SessionScope
from ingress_config import api_port, onebot_config, onebot_enabled, service_token
from memory.message_handler import MessageHandler
from memory.message_repository import MessageRepository
from paths import data_root
from utils import NetworkError, setup_logging

logger = setup_logging("ingress")
SHUTDOWN_EVENT = web.AppKey("ingress_shutdown", asyncio.Event)


class IngressRuntime:
    def __init__(self, channel=None, repository=None, bridge_config=None):
        self.channel = channel if channel is not None else (OneBotChannel(onebot_config()) if onebot_enabled() else None)
        self.repo = repository if repository is not None else MessageRepository()
        self.inbox = None
        self._inbox_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        bridge_config = bridge_config if bridge_config is not None else load_bridge_config()
        self.bridge = BridgeRuntime(bridge_config, self.repo, self._write_lock) if bridge_config.enabled else None
        self._wake = asyncio.Event()
        self._media_wake = asyncio.Event()
        self._tasks = []
        self.ready = False
        self.last_error = ""
        self._capture_error = ""
        self.started_at = time.time()

    async def start(self):
        try:
            await self.repo.ensure_ready()
            root = data_root() / "memory" / "chat_history"
            root.mkdir(parents=True, exist_ok=True)
            self.inbox = await aiosqlite.connect(root / "ingress_inbox.db")
            self.inbox.row_factory = aiosqlite.Row
            await self.inbox.execute("PRAGMA journal_mode=WAL")
            await self.inbox.execute("PRAGMA synchronous=FULL")
            await self.inbox.execute("PRAGMA busy_timeout=5000")
            await self.inbox.execute("""CREATE TABLE IF NOT EXISTS pending_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, raw TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', message_seq INTEGER,
                last_error TEXT NOT NULL DEFAULT '')""")
            # 上次处理失败的原始事件仍在，重启后重新尝试；不阻挡后续消息。
            await self.inbox.execute("UPDATE pending_events SET state='pending' WHERE state='failed'")
            await self.inbox.execute("UPDATE pending_events SET state='media' WHERE state='media_failed'")
            await self.inbox.commit()
            if self.channel is not None:
                self.channel.bind_raw_message_sink(self.capture)
                await self.channel.start()
            self._tasks = [asyncio.create_task(self._normalize_loop()), asyncio.create_task(self._media_loop())]
            if self.bridge:
                await self.bridge.start()
            self.ready = True
        except BaseException:
            await self.stop()
            raise

    async def stop(self):
        self.ready = False
        if self.bridge:
            await self.bridge.stop()
        # 先切断外部流入，再取消处理；未完成事件留在磁盘，下次继续。
        if self.channel is not None:
            await self.channel.stop()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self.inbox is not None:
            await self.inbox.close()
            self.inbox = None
        await self.repo.close()

    async def capture(self, raw):
        """只有 SQLite 提交完成才返回；不在接收循环下载媒体或调用大脑。"""
        raw = dict(raw)
        if not raw.get("time"):
            raw["time"] = int(time.time())
        async with self._inbox_lock:
            try:
                await self.inbox.execute("INSERT INTO pending_events(raw) VALUES (?)", (
                    json.dumps(raw, ensure_ascii=False),
                ))
                await self.inbox.commit()
                self._capture_error = ""
            except Exception as exc:
                self._capture_error = f"原始消息提交失败: {exc}"
                logger.exception(self._capture_error)
                await self.inbox.rollback()
                raise
        self._wake.set()

    def _healthy(self):
        return self.ready and not self._capture_error and all(not task.done() for task in self._tasks)

    async def _next(self, state):
        async with self._inbox_lock:
            cursor = await self.inbox.execute(
                "SELECT * FROM pending_events WHERE state=? ORDER BY id LIMIT 1", (state,))
            return await cursor.fetchone()

    async def _mark(self, row_id, state, seq=None, error=""):
        async with self._inbox_lock:
            if state == "done":
                await self.inbox.execute("DELETE FROM pending_events WHERE id=?", (row_id,))
            else:
                await self.inbox.execute(
                    "UPDATE pending_events SET state=?, message_seq=?, last_error=? WHERE id=?",
                    (state, seq, error, row_id))
            await self.inbox.commit()

    async def _normalize_loop(self):
        while True:
            self._wake.clear()
            row = await self._next("pending")
            if row is None:
                await self._wake.wait()
                continue
            try:
                event = parse_onebot_event(json.loads(row["raw"]))
                if event is None or not event.message_id:
                    raise ValueError("消息不能解析为具有 message_id 的私聊/群聊记录")
                if self.channel is not None and self.channel._recorder is not None:
                    try:
                        self.channel._recorder.record(category="message", raw=event.raw,
                            parsed=event.to_system_message_dict(), metadata={"durable_ingress": True})
                    except Exception:
                        logger.exception("原始事件调试录制失败；持久收件记录仍保留")
                # 即使原始事件没有 time，重放也使用首次接收时固定下来的时间。
                payload = MessageHandler.normalize_message_format(event.to_system_message_dict())
                async with self._write_lock:
                    result = await self.repo.store_message(payload)
                    if not isinstance(result, dict) or result.get("status") != "success":
                        raise RuntimeError("标准消息未成功落库")
                    conn = await self.repo._ensure_connection()
                    cursor = await conn.execute(
                        "SELECT seq FROM messages WHERE session_id=? AND timestamp=? "
                        "AND content_text=? AND message_id IN (?, ?) ORDER BY seq DESC LIMIT 1",
                        (event.session_id, event.timestamp, event.content_text,
                         event.message_id, f"{event.message_id}_{event.timestamp}"))
                    stored = await cursor.fetchone()
                    if stored is None:
                        raise RuntimeError("无法定位已落库的消息")
                media = self.channel is not None and self.channel._media_downloader is not None
                await self._mark(row["id"], "media" if media else "done", stored["seq"])
                self._media_wake.set()
            except Exception as exc:
                self.last_error = f"原始事件 {row['id']} 处理失败: {exc}"
                logger.exception(self.last_error)
                await self._mark(row["id"], "failed", error=str(exc))

    async def _media_loop(self):
        while True:
            self._media_wake.clear()
            row = await self._next("media")
            if row is None:
                await self._media_wake.wait()
                continue
            try:
                event = parse_onebot_event(json.loads(row["raw"]))
                if self.channel is not None and self.channel._media_downloader is not None:
                    await self.channel._media_downloader.enrich(event)
                    # 只补附件字段，保留 seq、原始正文和所有消费者游标。
                    async with self._write_lock:
                        conn = await self.repo._ensure_connection()
                        await conn.execute("UPDATE messages SET content=? WHERE seq=?", (
                            json.dumps([s.as_dict() for s in event.segments], ensure_ascii=False),
                            row["message_seq"],
                        ))
                        await conn.commit()
                await self._mark(row["id"], "done")
            except Exception as exc:
                # 正文已经落库。保留原始事件供重启重试，避免单条坏附件空转。
                self.last_error = f"媒体补充失败，正文已保留: {exc}"
                logger.exception(self.last_error)
                await self._mark(row["id"], "media_failed", row["message_seq"], str(exc))

    async def status(self):
        counts = {}
        if self.inbox is not None:
            async with self._inbox_lock:
                cursor = await self.inbox.execute("SELECT state, COUNT(*) AS n FROM pending_events GROUP BY state")
                counts = {row["state"]: row["n"] for row in await cursor.fetchall()}
        return {"service": "abyss-ingress", "instance": str(data_root()), "ready": self._healthy(),
                "onebot_enabled": self.channel is not None,
                "onebot_connected": bool(self.channel and self.channel.is_connected()),
                "bridge": await self.bridge.status() if self.bridge else {"enabled": False},
                "pending": counts, "last_error": self._capture_error or self.last_error,
                "uptime_sec": round(time.time() - self.started_at, 1)}


def build_app(runtime=None):
    runtime = runtime if runtime is not None else IngressRuntime()
    token = service_token()

    @web.middleware
    async def authorize(request, handler):
        if request.path != "/health" and request.headers.get("X-Manager-Token") != token:
            return web.json_response({"error": "管理令牌缺失或不匹配"}, status=401)
        try:
            return await handler(request)
        except (ValueError, TypeError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except NetworkError as exc:
            return web.json_response({"error": str(exc)}, status=503)

    app = web.Application(middlewares=[authorize])

    async def lifecycle(app):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    async def health(request):
        ready = runtime._healthy()
        return web.json_response({"service": "abyss-ingress", "ready": ready}, status=200 if ready else 503)

    async def status(request):
        return web.json_response(await runtime.status())

    async def api(request):
        body = await request.json()
        if runtime.channel is None:
            raise NetworkError("OneBot 已禁用")
        if not isinstance(body.get("action"), str) or not isinstance(body.get("params"), dict):
            raise ValueError("action 必须为字符串，params 必须为对象")
        return web.json_response(await runtime.channel.call_api(body["action"], body["params"]))

    async def send(request):
        body = await request.json()
        if runtime.channel is None:
            raise NetworkError("OneBot 已禁用")
        target = OutboundTarget(SessionScope(body["scope"]), str(body["target_id"]))
        segments = [MessageSegment(item["type"], item["data"]) for item in body["segments"]]
        if not target.target_id or not segments:
            raise ValueError("目标与消息不能为空")
        message_id = await runtime.channel.send(target, OutboundPayload(segments=segments))
        if not message_id:
            raise NetworkError("平台发送回执缺少 message_id")
        # 接入进程持有发送与落库，Core 收到回执前退出也不影响这一步。
        async with runtime._write_lock:
            # 本服务不初始化全局 MessageHandler；仅复用标准化与仓库。
            from config.role_settings import role_config
            sid = f"{target.scope.value}_{target.target_id}"
            payload = {"session_info": {"session_id": sid, "platform": "qq", "connection_id": "onebot",
                                         "remote_session_id": sid, "generation": ""},
                       "message_list": [{"message_id": message_id, "user_id": "system",
                           "user_name": role_config.ASSISTANT_NAME, "timestamp": int(time.time()),
                           "content_text": "".join(s.to_text() for s in segments),
                           "content": [s.as_dict() for s in segments]}]}
            result = await runtime.repo.store_message(MessageHandler.normalize_message_format(payload))
            if not isinstance(result, dict) or result.get("status") != "success":
                raise RuntimeError("已发送消息未成功落库，请先查日志，避免重复发送")
        return web.json_response({"message_id": message_id})

    async def shutdown(request):
        asyncio.get_running_loop().call_later(0.2, lambda: app[SHUTDOWN_EVENT].set())
        return web.json_response({"ok": True})

    async def bridge_send(request):
        if runtime.bridge is None:
            raise NetworkError("通用桥接已禁用")
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("request_id"), str):
            raise ValueError("request_id 必须为字符串")
        return web.json_response(asdict(await runtime.bridge.submit(body["request_id"])))

    app.cleanup_ctx.append(lifecycle)
    app[SHUTDOWN_EVENT] = asyncio.Event()
    app.router.add_get("/health", health)
    app.router.add_get("/v1/status", status)
    app.router.add_post("/v1/onebot-api", api)
    app.router.add_post("/v1/send", send)
    app.router.add_post("/v1/bridge/send", bridge_send)
    app.router.add_post("/v1/shutdown", shutdown)
    return app


async def run_service(port=None):
    app = build_app()
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", port if port is not None else api_port())
        await site.start()
        logger.info("消息接入服务已启动；停止 Core 不会停止接收与落库")
        await app[SHUTDOWN_EVENT].wait()
    finally:
        await runner.cleanup()
