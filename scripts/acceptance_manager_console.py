"""独立管理页面验收：真实 HTTP/WS + 临时数据目录，不启动 core 或 LLM。"""

import json
import os
import runpy
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from manager_console import register_console
from truck_settings_store import TRUCKS, apply_startup_settings, read_settings, update_settings


class Controller:
    def __init__(self, name):
        self.value = SimpleNamespace(name=name, state="stopped", desired="stopped", pid=None,
                                     ports=[5002, 5001] if name == "core" else [5099],
                                     ready=False, managed=True, last_error="", uptime_sec=None)
        self.commands = []

    def snapshot(self):
        return self.value

    def submit(self, action):
        self.commands.append(action)
        return {"ok": False, "message": "模拟 core 启动失败"}


class ConsoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"ABYSS_DATA_DIR": str(self.root)})
        self.env.start()
        dist = self.root / "web_frontend/dist"
        dist.mkdir(parents=True)
        (dist / "index.html").write_text("console-fixture", encoding="utf-8")
        self.controllers = {name: Controller(name) for name in ("core", "snowluma")}
        self.service = SimpleNamespace(controller=self.controllers.get)
        app = web.Application()
        self.console = register_console(app, self.service, "test-manager-token")
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"X-Manager-Token": "test-manager-token"}
        self.upstream = None

    async def asyncTearDown(self):
        await self.client.close()
        if self.upstream:
            await self.upstream.close()
        self.env.stop()
        self.tmp.cleanup()

    async def test_page_and_configuration_work_without_core(self):
        self.assertEqual((await self.client.get("/console/trucks")).status, 401)
        response = await self.client.post("/console/session", headers=self.headers)
        self.assertEqual(response.status, 200)
        body = await response.json()
        cookie = {"Cookie": f"abyss_console_session={body['session']}"}
        self.assertEqual(await (await self.client.get("/", headers=cookie)).text(), "console-fixture")
        response = await self.client.put("/console/trucks", headers=cookie,
                                         json={"enabled": {"task_truck": True}, "startup_paused": True})
        data = await response.json()
        self.assertEqual(response.status, 200)
        self.assertTrue(data["enabled"]["task_truck"])
        self.assertTrue(data["startup_paused"])
        self.assertFalse(data["core_ready"])
        self.assertTrue(all(t["runtime_state"] == "stopped" and not t["enabled"] for t in data["trucks"]))
        self.assertEqual(self.controllers["core"].commands, [])
        self.assertTrue(read_settings()["enabled"]["task_truck"])
        core_config = runpy.run_path(str(ROOT / "config/truck_settings.py"))
        apply_startup_settings(core_config["get_all_truck_configs"]())
        self.assertTrue(all(not cfg.ENABLED for cfg in core_config["get_all_truck_configs"]().values()))

    async def test_saved_config_is_loaded_before_station_start(self):
        update_settings({"enabled": {key: False for key in TRUCKS}})
        update_settings({"enabled": {"task_truck": True}})
        config = runpy.run_path(str(ROOT / "config/truck_settings.py"))
        apply_startup_settings(config["get_all_truck_configs"]())
        enabled = {key: cfg.ENABLED for key, cfg in config["get_all_truck_configs"]().items()}
        self.assertEqual([key for key, value in enabled.items() if value], ["task_truck"])

    async def test_context_reads_are_side_effect_free_and_confined(self):
        chain = self.root / "memory/event_chains/example"
        chain.mkdir(parents=True)
        path = chain / "pfca.json"
        path.write_text(json.dumps([{"role": "assistant", "content": "历史内容", "tool_calls": []}]), encoding="utf-8")
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        response = await self.client.get("/context/chains", headers=self.headers)
        self.assertEqual((await response.json())["chains"][0]["chain_id"], "example")
        response = await self.client.get("/context/file?chain=example&name=pfca.json", headers=self.headers)
        self.assertEqual((await response.json())["entries"][0]["content"], "历史内容")
        response = await self.client.get("/context/file?chain=..&name=pfca.json", headers=self.headers)
        self.assertEqual(response.status, 400)
        after = {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(any(name.startswith("brain.") for name in sys.modules))

    async def test_failed_core_does_not_disable_console(self):
        response = await self.client.post("/console/core/start", headers=self.headers)
        self.assertEqual(response.status, 409)
        self.controllers["core"].value.state = "failed"
        self.controllers["core"].value.last_error = "模拟启动失败"
        response = await self.client.get("/console/status", headers=self.headers)
        self.assertFalse((await response.json())["core_ready"])
        self.assertEqual((await self.client.get("/console/trucks", headers=self.headers)).status, 200)
        self.assertEqual((await self.client.get("/", headers=self.headers)).status, 200)
        self.assertEqual((await self.client.post("/console/trucks/apply", headers=self.headers)).status, 503)

    async def test_invalid_changes_and_cross_site_requests_are_rejected(self):
        for payload in ({"enabled": {"task_truck": "false"}}, {"enabled": {"unknown": True}}, {"startup_paused": "yes"}):
            response = await self.client.put("/console/trucks", headers=self.headers, json=payload)
            self.assertEqual(response.status, 400)
        self.assertFalse((self.root / "manager/trucks.json").exists())
        response = await self.client.put("/console/trucks", headers={**self.headers, "Origin": "https://untrusted.example"}, json={})
        self.assertEqual(response.status, 403)

    async def test_offline_history_uses_first_alias_and_read_only_database(self):
        folder = self.root / "memory/chat_history"
        folder.mkdir(parents=True)
        db = folder / "chat_history.db"
        with sqlite3.connect(db) as conn:
            conn.executescript("CREATE TABLE sessions (session_id TEXT, session_name TEXT, last_update_time REAL);"
                               "CREATE TABLE messages (seq INTEGER, session_id TEXT, message_id TEXT, content TEXT, content_text TEXT);")
            conn.execute("INSERT INTO sessions VALUES ('private_123', '临时身份', 1)")
            conn.execute("INSERT INTO messages VALUES (1, 'private_123', 'm1', '[]', '已保存的消息')")
        conn.close()
        with sqlite3.connect(self.root / "memory/memory.db") as conn:
            for table in ("characters", "groups"):
                conn.execute(f"CREATE TABLE {table} (uuid TEXT, name TEXT, aliases TEXT, contacts TEXT)")
            conn.execute("INSERT INTO characters VALUES (?, ?, ?, ?)",
                         ("c1", "主名", json.dumps(["第一个别名", "临时身份"]), json.dumps([{"session_id": "private_123"}])))
        conn.close()
        before = db.read_bytes()
        data = await (await self.client.get("/sessions", headers=self.headers)).json()
        self.assertEqual(data["sessions"][0]["session_name"], "第一个别名")
        data = await (await self.client.get("/messages?scope=private&id=123", headers=self.headers)).json()
        self.assertEqual(data["messages"][0]["content_text"], "已保存的消息")
        self.assertEqual(db.read_bytes(), before)

    async def test_http_and_websocket_proxy_recover_when_core_is_ready(self):
        upstream_app = web.Application()

        async def trucks(request):
            self.assertEqual(request.headers.get("X-Manager-Token"), "test-manager-token")
            return web.json_response({"core_ready": True, "trucks": [{"runtime_state": "running"}]})

        async def websocket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_json({"type": "hello"})
            async for message in ws:
                await ws.send_str(message.data)
            return ws

        upstream_app.router.add_get("/console/trucks", trucks)
        upstream_app.router.add_get("/web/ws", websocket)
        self.upstream = TestServer(upstream_app)
        await self.upstream.start_server()
        core = self.controllers["core"].value
        core.state, core.ready, core.ports = "running", True, [self.upstream.port]
        response = await self.client.get("/console/trucks", headers=self.headers)
        self.assertTrue((await response.json())["core_ready"])
        ws = await self.client.ws_connect("/web/ws", headers=self.headers)
        self.assertEqual((await ws.receive_json())["type"], "hello")
        await ws.send_str("echo-fixture")
        self.assertEqual((await ws.receive()).data, "echo-fixture")
        await ws.close()
        core.state, core.ready = "stopped", False
        self.assertEqual((await self.client.get("/console/trucks", headers=self.headers)).status, 200)

    async def test_saved_settings_are_acknowledged_if_core_disconnects(self):
        app = web.Application()
        app.router.add_get("/console/trucks", lambda request: web.json_response({"error": "unavailable"}, status=503))
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        core = self.controllers["core"].value
        core.state, core.ready, core.ports = "running", True, [self.upstream.port]
        response = await self.client.put("/console/trucks", headers=self.headers, json={"enabled": {"task_truck": True}})
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertTrue(data["enabled"]["task_truck"])
        self.assertTrue(all(t["runtime_state"] == "unknown" for t in data["trucks"]))

    async def test_offline_media_does_not_need_core(self):
        folder = self.root / "memory/chat_history/media/images"
        folder.mkdir(parents=True)
        (folder / "fixture.png").write_bytes(b"image-fixture")
        response = await self.client.get("/media/images/fixture.png", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), b"image-fixture")
        self.assertEqual(self.controllers["core"].commands, [])


if __name__ == "__main__":
    unittest.main()
