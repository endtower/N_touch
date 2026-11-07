"""Human platform settings/probe/restart tests: isolated configuration and mock controllers."""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from acceptance_bridge_contract import BridgeFixture, TEMP_ROOT, TEST_DATA
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="platform_api_", dir=TEMP_ROOT)
        self.env = patch.dict(os.environ, {"ABYSS_DATA_DIR": self.tmp.name})
        self.env.start()
        self.peer = BridgeFixture()
        self.peer_server = TestServer(self.peer.app)
        await self.peer_server.start_server()
        self.path = Path(self.tmp.name, ".env.dev")
        self.path.write_text("ONEBOT_ENABLED=true\nBRIDGE_ENABLED=false\n"
                             "BRIDGE_TOKEN=synthetic-test-token\n"
                             f"BRIDGE_BASE_URL={self.peer_server.make_url('/')}\n", encoding="utf-8")
        self.manager = importlib.import_module("manager_service")
        self.manager._connection_apply.update(running=False, phase="", error="")
        app = web.Application()
        app.router.add_get("/config", self.manager.handle_config_get)
        app.router.add_put("/config", self.manager.handle_config_put)
        app.router.add_post("/check", self.manager.handle_connection_check)
        app.router.add_post("/apply", self.manager.handle_connections_apply)
        self.http = TestClient(TestServer(app))
        await self.http.start_server()
        self.headers = {"X-Manager-Token": self.manager.TOKEN}

    async def asyncTearDown(self):
        task = self.manager._connection_apply_task
        if task and not task.done():
            await asyncio.wait_for(task, 3)
        await self.http.close()
        await self.peer_server.close()
        os.chdir(ROOT)
        self.env.stop()
        logging.shutdown()
        self.tmp.cleanup()

    async def put(self, fields):
        return await self.http.put("/config", headers=self.headers, json={"fields": fields})

    async def test_flags_masked_secrets_and_draft_enable(self):
        body = await (await self.http.get("/config", headers=self.headers)).json()
        self.assertIs(body["fields"]["ONEBOT_ENABLED"], True)
        self.assertIs(body["fields"]["BRIDGE_ENABLED"], False)
        self.assertTrue(body["fields"]["BRIDGE_TOKEN_SET"])
        self.assertNotIn("synthetic-test-token", str(body))
        self.assertEqual((await self.put({"BRIDGE_ENABLED": True})).status, 200)
        self.assertIn("BRIDGE_ENABLED=true", self.path.read_text())
        self.assertIn("BRIDGE_TOKEN=synthetic-test-token", self.path.read_text())

    async def test_enable_without_token_and_invalid_values_do_not_write(self):
        for fields in ({"BRIDGE_ENABLED": True, "BRIDGE_TOKEN": ""},
                       {"BRIDGE_ENABLED": "sometimes"},
                       {"BRIDGE_BASE_URL": "http://user:password@localhost:8770"},
                       {"BRIDGE_TOKEN": "token\nINJECT=true"}):
            before = self.path.read_bytes()
            self.assertEqual((await self.put(fields)).status, 400)
            self.assertEqual(self.path.read_bytes(), before)

    async def test_probe_reads_health_only_and_does_not_persist_draft(self):
        before = self.path.read_bytes()
        response = await self.http.post("/check", headers=self.headers, json={
            "BRIDGE_BASE_URL": str(self.peer_server.make_url("/"))})
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["ok"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.peer.posts, [])
        self.assertEqual(self.peer.poll_queries, [])

    async def test_probe_rejects_bad_auth_and_legacy_capabilities(self):
        response = await self.http.post("/check", headers=self.headers, json={"BRIDGE_TOKEN": "incorrect"})
        self.assertEqual(response.status, 400)
        self.assertNotIn("incorrect", str(await response.json()))
        self.peer.capabilities = []
        response = await self.http.post("/check", headers=self.headers, json={})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.peer.posts, [])

    async def test_auth_required_for_check_and_apply(self):
        self.assertEqual((await self.http.post("/check", json={})).status, 401)
        self.assertEqual((await self.http.post("/apply")).status, 401)

    async def test_console_checks_bridge_without_core_and_preserves_auth(self):
        from manager_console import register_console
        app = web.Application()
        register_console(app, SimpleNamespace(controller=lambda name: None), self.manager.TOKEN,
                         connection_handlers={"/system/connections/check": self.manager.handle_connection_check})
        async with TestClient(TestServer(app)) as client:
            self.assertEqual((await client.post("/system/connections/check", json={})).status, 401)
            response = await client.post("/system/connections/check", headers=self.headers, json={})
            self.assertEqual(response.status, 200)
            self.assertTrue((await response.json())["ok"])

    async def test_apply_restarts_in_correct_order_without_real_processes(self):
        calls = []
        controllers = {name: Mock() for name in ("core", "ingress")}
        for name, ctrl in controllers.items():
            ctrl.submit.side_effect = lambda action, name=name: calls.append((name, action)) or {"ok": True}
        fake = SimpleNamespace(update_guard=None, controller=lambda name: controllers[name])
        with patch.object(self.manager, "_service", fake):
            response = await self.http.post("/apply", headers=self.headers)
            self.assertEqual(response.status, 200)
            again = await self.http.post("/apply", headers=self.headers)
            self.assertEqual(again.status, 409)
            await self.manager._connection_apply_task
        self.assertEqual(calls, [("core", "stop"), ("ingress", "restart"), ("core", "start")])
        self.assertEqual(self.manager._connection_apply["phase"], "已应用")

    async def test_failed_apply_does_not_start_core_and_reports_failure(self):
        calls = []
        def submit(name, action):
            calls.append((name, action))
            return {"ok": False, "message": "synthetic ingress failure"} if name == "ingress" else {"ok": True}
        fake = SimpleNamespace(update_guard=None, controller=lambda name: SimpleNamespace(
            submit=lambda action: submit(name, action)))
        with patch.object(self.manager, "_service", fake):
            await self.http.post("/apply", headers=self.headers)
            await self.manager._connection_apply_task
        self.assertEqual(calls, [("core", "stop"), ("ingress", "restart")])
        self.assertEqual(self.manager._connection_apply["error"], "synthetic ingress failure")
        self.assertFalse(self.manager._connection_apply["running"])


if __name__ == "__main__":
    try:
        result = unittest.main(verbosity=2, exit=False).result
    finally:
        logging.shutdown()
        TEST_DATA.cleanup()
    sys.exit(0 if result.wasSuccessful() else 1)
