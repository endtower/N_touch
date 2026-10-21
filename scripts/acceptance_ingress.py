"""独立接入验收；只使用临时数据库、随机本机端口和模拟 OneBot，不连接 QQ/LLM。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TEST_DATA = tempfile.TemporaryDirectory(prefix="abyss_ingress_acceptance_")
os.environ["ABYSS_DATA_DIR"] = TEST_DATA.name
Path(TEST_DATA.name, ".env.dev").write_text("ONEBOT_ENABLED=true\n", encoding="utf-8")

from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from adapt.ingress_runtime import IngressRuntime, build_app
from adapt.ingress_client import IngressChannel
from adapt.onebot.channel import OneBotChannel
from adapt.onebot.config import OneBotChannelConfig
from memory.message_repository import MessageRepository
import ingress_config


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def event(mid="1", text="hello", timestamp=100):
    return {"post_type": "message", "message_type": "private", "user_id": 123,
            "message_id": mid, "time": timestamp, "sender": {"nickname": "测试用户"},
            "message": [{"type": "text", "data": {"text": text}}]}


async def until(predicate, timeout=8):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待条件超时")


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="abyss_ingress_case_")
        self.env = patch.dict(os.environ, {"ABYSS_DATA_DIR": self.tmp.name})
        self.env.start()
        Path(self.tmp.name, ".env.dev").write_text("ONEBOT_ENABLED=true\n", encoding="utf-8")
        self.channel = OneBotChannel(OneBotChannelConfig(port=free_port(), media_download_enabled=False))
        self.runtime = IngressRuntime(self.channel, MessageRepository())
        self.client = TestClient(TestServer(build_app(self.runtime)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        logging.shutdown()
        self.env.stop()
        self.tmp.cleanup()

    async def messages(self):
        return (await self.runtime.repo.read_session_messages("private_123"))["message_list"]

    async def count_is(self, n):
        return len(await self.messages()) == n

    async def test_websocket_capture_deduplicates_and_preserves_cursor(self):
        async with ClientSession() as client:
            async with client.ws_connect(f"http://127.0.0.1:{self.channel.config.port}/onebot/v11/ws") as ws:
                await ws.send_json(event())
                await ws.send_json(event())
                await until(lambda: self.count_is(1))
                messages = await self.messages()
                await self.runtime.repo.advance_cursor("private_123", "pfca", messages[0]["_seq"])
                await ws.send_json(event("2", "later", 101))
                await until(lambda: self.count_is(2))
                unread = await self.runtime.repo.get_unread_for("private_123", "pfca")
                self.assertEqual([m["message_id"] for m in unread], ["2"])

    async def test_raw_capture_survives_restart_before_normalization(self):
        task = self.runtime._tasks[0]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.runtime.capture(event())
        self.assertEqual((await self.runtime.status())["pending"], {"pending": 1})
        await self.client.close()
        self.channel = OneBotChannel(OneBotChannelConfig(port=free_port(), media_download_enabled=False))
        self.runtime = IngressRuntime(self.channel, MessageRepository())
        self.client = TestClient(TestServer(build_app(self.runtime)))
        await self.client.start_server()
        await until(lambda: self.count_is(1))

    async def test_replay_after_message_commit_does_not_duplicate(self):
        await self.runtime.capture(event())
        await until(lambda: self.count_is(1))
        await self.runtime.capture(event())
        async def drained():
            return not (await self.runtime.status())["pending"]
        await until(drained)
        self.assertEqual(len(await self.messages()), 1)

    async def test_slow_media_does_not_block_following_messages(self):
        release = asyncio.Event()
        started = asyncio.Event()
        async def enrich(ev):
            started.set()
            await release.wait()
            ev.segments[0].data["local_path"] = "test.png"
        self.channel._media_downloader = type("Media", (), {
            "enrich": staticmethod(enrich), "close": AsyncMock(),
        })()
        await self.runtime.capture(event("1"))
        await started.wait()
        await self.runtime.capture(event("2", "following", 101))
        await until(lambda: self.count_is(2))
        release.set()
        async def drained():
            return not (await self.runtime.status())["pending"]
        await until(drained)
        self.assertIn("local_path", (await self.messages())[0]["content"][0]["data"])

    async def test_malformed_event_is_retained_and_later_message_is_processed(self):
        malformed = event()
        malformed["user_id"] = ""
        await self.runtime.capture(malformed)
        await self.runtime.capture(event("2"))
        await until(lambda: self.count_is(1))
        self.assertEqual((await self.runtime.status())["pending"], {"failed": 1})

    async def test_api_authentication_and_offline_send_failure(self):
        self.assertEqual((await self.client.get("/v1/status")).status, 401)
        self.assertEqual((await self.client.post("/v1/shutdown")).status, 401)
        headers = {"X-Manager-Token": ingress_config.service_token()}
        response = await self.client.post("/v1/send", headers=headers, json={
            "scope": "private", "target_id": "123", "segments": [{"type": "text", "data": {"text": "reply"}}],
        })
        self.assertEqual(response.status, 503)
        self.assertEqual(await self.messages(), [])

    async def test_proxy_send_and_shutdown_do_not_stop_ingress(self):
        self.channel.send = AsyncMock(return_value="out-1")
        proxy = IngressChannel()
        proxy._base = str(self.client.make_url("" )).rstrip("/")
        await proxy.start()
        from adapt.protocol import OutboundPayload, OutboundTarget, SessionScope
        self.assertEqual(await proxy.send(OutboundTarget(SessionScope.PRIVATE, "123"), OutboundPayload.text("reply")), "out-1")
        await proxy.stop()
        self.assertTrue(self.runtime.ready)
        self.assertEqual((await self.messages())[0]["user_id"], "system")
        self.assertNotIn("brain.att", sys.modules)
        self.assertNotIn("brain.amy", sys.modules)

    async def test_core_router_does_not_write_again_after_remote_send(self):
        from adapt.router import OutboundRouter
        from adapt.protocol import OutboundPayload, OutboundTarget, SessionScope
        router = OutboundRouter()
        proxy = IngressChannel()
        proxy.send = AsyncMock(return_value="out-1")
        await router.register(proxy)
        router._persist_outbound = AsyncMock()
        with patch("config.settings.safety_config.SIMULATED_SEND", False):
            await router.send("qq", OutboundTarget(SessionScope.PRIVATE, "123"), OutboundPayload.text("reply"))
        router._persist_outbound.assert_not_awaited()

    async def test_proxy_send_roundtrip_while_inbound_messages_arrive(self):
        proxy = IngressChannel()
        proxy._base = str(self.client.make_url("")).rstrip("/")
        await proxy.start()
        from adapt.protocol import OutboundPayload, OutboundTarget, SessionScope
        try:
            async with ClientSession() as client:
                async with client.ws_connect(f"http://127.0.0.1:{self.channel.config.port}/onebot/v11/ws") as ws:
                    send_task = asyncio.create_task(proxy.send(
                        OutboundTarget(SessionScope.PRIVATE, "123"), OutboundPayload.text("reply")))
                    request = await ws.receive_json(timeout=5)
                    self.assertEqual(request["action"], "send_private_msg")
                    await ws.send_json(event("in-1"))
                    await ws.send_json({"echo": request["echo"], "retcode": 0, "data": {"message_id": "out-1"}})
                    self.assertEqual(await send_task, "out-1")
                    await until(lambda: self.count_is(2))
                    self.assertEqual({m["message_id"] for m in await self.messages()}, {"in-1", "out-1"})
        finally:
            await proxy.stop()


class ManagementTests(unittest.TestCase):
    def test_core_start_starts_ingress_and_core_stop_leaves_ingress_running(self):
        from manager_core import SupervisorService, TargetController
        from core.supervisor import ProcSpec
        from unittest.mock import Mock
        import threading
        with tempfile.TemporaryDirectory(prefix="abyss_ingress_manager_") as tmp:
            service = SimpleNamespace(epoch_lock=threading.Lock())
            ingress = Mock(ready=True)
            ingress.submit.return_value = {"ok": True}
            service.controller = lambda name: ingress if name == "ingress" else None
            spec = ProcSpec("core", ["fake"], Path(tmp), Path(tmp, "core.log"), [65531],
                            "http://test.invalid/health", dependencies=("ingress",))
            with patch("threading.Thread.start"):
                controller = TargetController(spec, service)
            controller.desired = "running"
            process = Mock(pid=1, returncode=None)
            process.poll.return_value = None
            with patch.object(controller, "_refresh_spec"), patch("manager_core._port_alive", return_value=False), \
                    patch("manager_core._health_ok", return_value=True), patch("manager_core.spawn", return_value=process):
                self.assertTrue(controller._cmd_start(0)["ok"])
                ingress.submit.assert_called_once_with("start")
                process.poll.return_value = 0
                self.assertTrue(controller._cmd_stop(0)["ok"])
                ingress.submit.assert_called_once_with("start")

    def test_stop_all_stops_ingress_last(self):
        from manager_core import SupervisorService
        from unittest.mock import Mock
        service = SupervisorService.__new__(SupervisorService)
        calls = []
        service.controllers = {}
        for name, order in (("ingress", 0), ("snowluma", 1), ("core", 2)):
            controller = Mock()
            controller.spec = SimpleNamespace(name=name, order=order)
            controller.submit.side_effect = lambda action, name=name: calls.append(name) or {"message": "stopped"}
            service.controllers[name] = controller
        service.stop_everything()
        self.assertEqual(calls, ["core", "snowluma", "ingress"])


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_manager_manages_independent_ingress_and_stops_it_cleanly(self):
        with tempfile.TemporaryDirectory(prefix="abyss_ingress_manager_process_") as temp:
            manager_port, ingress_port, ob_port, web_port = free_port(), free_port(), free_port(), free_port()
            Path(temp, ".env.dev").write_text(
                f"INGRESS_API_PORT={ingress_port}\nONEBOT_WS_PORT={ob_port}\nWEB_WS_PORT={web_port}\n", encoding="utf-8")
            env = os.environ.copy()
            env["ABYSS_DATA_DIR"] = temp
            from config_keys import CONFIG_OVERRIDE_KEYS
            for key in CONFIG_OVERRIDE_KEYS:
                env.pop(key, None)
            package = os.getenv("ABYSS_ACCEPTANCE_PACKAGE")
            command = ([str(Path(package, "manager.exe"))] if package else [sys.executable, "-u", str(ROOT / "manager_service.py")])
            process = subprocess.Popen(command + ["--port", str(manager_port)], cwd=ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000 if os.name == "nt" else 0)
            try:
                async with ClientSession() as client:
                    base = f"http://127.0.0.1:{manager_port}"
                    token_path = Path(temp, "manager/service_token.txt")
                    async def ready():
                        if process.poll() is not None:
                            log = Path(temp, "logs/manager/service.out.log")
                            raise AssertionError(log.read_text(encoding="utf-8", errors="replace") if log.exists() else "管理服务退出")
                        if not token_path.is_file():
                            return False
                        try:
                            async with client.get(base + "/v1/info", headers={"X-Manager-Token": token_path.read_text().strip()}) as response:
                                return response.status == 200
                        except Exception:
                            return False
                    await until(ready, 30)
                    headers = {"X-Manager-Token": token_path.read_text().strip()}
                    async with client.post(base + "/v1/targets/ingress/start", headers=headers) as response:
                        self.assertTrue((await response.json())["ok"])
                    async with client.get(base + "/v1/targets", headers=headers) as response:
                        targets = {t["name"]: t for t in (await response.json())["targets"]}
                    self.assertEqual(set(targets), {"core", "snowluma", "ingress"})
                    self.assertTrue(targets["ingress"]["ready"])
                    self.assertFalse(targets["ingress"]["onebot_connected"])
                    self.assertNotIn(ob_port, targets["core"]["ports"])
                    async with client.post(base + "/v1/targets/ingress/stop", headers=headers) as response:
                        self.assertTrue((await response.json())["ok"])
                    async with client.get(base + "/v1/targets", headers=headers) as response:
                        targets = {t["name"]: t for t in (await response.json())["targets"]}
                    self.assertEqual(targets["ingress"]["state"], "stopped")
                    self.assertIsNone(targets["ingress"]["pid"])
                    async with client.post(base + "/v1/shutdown", headers=headers) as response:
                        self.assertEqual(response.status, 200)
                    await asyncio.to_thread(process.wait, 10)
            finally:
                if process.poll() is None:
                    # 只终止本测试创建并持有的管理进程树。
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=0x08000000)
                    else:
                        process.kill()
                    await asyncio.to_thread(process.wait, 10)

    async def test_core_process_crash_and_restart_do_not_disconnect_ingress(self):
        with tempfile.TemporaryDirectory(prefix="abyss_ingress_process_") as temp:
            port, ob_port, web_port, main_port = free_port(), free_port(), free_port(), free_port()
            Path(temp, ".env.dev").write_text(
                f"INGRESS_API_PORT={port}\nONEBOT_WS_PORT={ob_port}\nWEB_WS_PORT={web_port}\n"
                "ONEBOT_ACCESS_TOKEN=acceptance-local\n", encoding="utf-8")
            persona_dir = Path(temp, "brain/base/persona")
            persona_dir.mkdir(parents=True)
            for name in ("common", "normal"):
                (persona_dir / f"{name}.json").write_text(json.dumps({
                    "persona_name": name, "scene": "隔离验收", "kernel_prompt": "验收占位",
                }), encoding="utf-8")
            manager_dir = Path(temp, "manager")
            manager_dir.mkdir()
            (manager_dir / "trucks.json").write_text(json.dumps({
                "version": 1, "startup_paused": True, "enabled": {},
            }), encoding="utf-8")
            env = os.environ.copy()
            env.update({"ABYSS_DATA_DIR": temp, "INGRESS_API_PORT": str(port),
                        "ONEBOT_WS_PORT": str(ob_port), "ONEBOT_ACCESS_TOKEN": "acceptance-local",
                        "WEB_WS_PORT": str(web_port), "OPENCODE_API_KEY": "", "FLASH_API_KEY": "",
                        "CONVERSATION_API_KEY": "", "PERSONA_API_KEY": ""})
            output = open(Path(temp, "process.log"), "wb")
            package = os.getenv("ABYSS_ACCEPTANCE_PACKAGE")
            ingress_command = [str(Path(package, "ingress.exe"))] if package else [sys.executable, "-u", str(ROOT / "ingress_service.py")]
            core_command = ([str(Path(package, "core.exe"))] if package else [sys.executable, "-u", str(ROOT / "main.py")]) + ["--port", str(main_port)]
            ingress = subprocess.Popen(ingress_command,
                cwd=ROOT, env=env, stdout=output, stderr=output,
                creationflags=0x08000000 if os.name == "nt" else 0)
            consumer = None
            repository = MessageRepository(str(Path(temp, "memory/chat_history")))
            try:
                async with ClientSession() as client:
                    async def ready():
                        if ingress.poll() is not None:
                            raise AssertionError(Path(temp, "process.log").read_text(encoding="utf-8", errors="replace"))
                        try:
                            async with client.get(f"http://127.0.0.1:{port}/health") as response:
                                return response.status == 200
                        except Exception:
                            return False
                    await until(ready, 30)
                    await repository.ensure_ready()
                    # 启动真实 Core，调度器全部暂停，禁止任何 LLM 调用。
                    consumer = subprocess.Popen(core_command, cwd=ROOT,
                        env=env, stdout=output, stderr=output,
                        creationflags=0x08000000 if os.name == "nt" else 0)
                    async def core_ready():
                        if consumer.poll() is not None:
                            paths = [Path(temp, "process.log"), Path(temp, "logs/core_stdout.log")]
                            raise AssertionError("\n".join(p.read_text(encoding="utf-8", errors="replace")[-6000:] for p in paths if p.exists()))
                        try:
                            async with client.get(f"http://127.0.0.1:{web_port}/health") as response:
                                return response.status == 200
                        except Exception:
                            return False
                    await until(core_ready, 30)
                    # Verify the frozen Core contains the actual-benefit scoring rule.
                    # All requests target this paused, isolated fixture; no model calls.
                    async with client.post(f"http://127.0.0.1:{web_port}/amy/final-evaluation", json={
                        "title": "旧成败口径", "final_evaluation": "无评价", "task_result": "成功",
                        "failure_reason": "完成任务不能自动证明效益",
                    }) as response:
                        self.assertEqual(response.status, 400, await response.text())
                    expected = 50
                    for result, reason, delta in (
                        ("对方明确说自己有用/产生了实际价值/明确使对方收益", "主人明确说资料正是他要的", 10),
                        ("可能产生收益", "有迹象可能有用，但说不清具体收益", 0),
                        ("结果不满", "主人对这次的处理表示不满", -5),
                        ("任务延期", "任务被推迟，尚未交付", -10),
                        ("产生负面收益", "提供的错误信息使主人返工", -20),
                        ("帮倒忙", "错误操作使已完成的进度丢失", -30),
                    ):
                        async with client.post(f"http://127.0.0.1:{web_port}/amy/final-evaluation", json={
                            "title": "隔离效益验收", "final_evaluation": "无评价", "task_result": result, "failure_reason": reason,
                        }) as response:
                            self.assertEqual(response.status, 200, await response.text())
                        expected += delta
                        async with client.get(f"http://127.0.0.1:{web_port}/amy/state") as response:
                            state = (await response.json())["decision_feedback"]
                            self.assertEqual(state["competence_value"], expected)
                            self.assertEqual(state["task_results"][-1]["result"], result)
                    async with client.ws_connect(f"http://127.0.0.1:{ob_port}/onebot/v11/ws?access_token=acceptance-local") as ws:
                        await ws.send_json(event("1"))
                        async def one():
                            return len((await repository.read_session_messages("private_123"))["message_list"]) == 1
                        await until(one)
                        first = (await repository.read_session_messages("private_123"))["message_list"][0]
                        await repository.advance_cursor("private_123", "pfca", first["_seq"])
                        consumer.kill()
                        await asyncio.to_thread(consumer.wait, 5)
                        await ws.send_json(event("2", "after crash", 101))
                        async def two():
                            return len((await repository.read_session_messages("private_123"))["message_list"]) == 2
                        await until(two)
                        self.assertIsNone(ingress.poll())
                        self.assertFalse(ws.closed)
                        self.assertEqual([m["message_id"] for m in await repository.get_unread_for("private_123", "pfca")], ["2"])
                        consumer = subprocess.Popen(core_command, cwd=ROOT, env=env,
                            stdout=output, stderr=output, creationflags=0x08000000 if os.name == "nt" else 0)
                        await until(core_ready, 30)
                        self.assertFalse(ws.closed)
                        async with client.get(f"http://127.0.0.1:{web_port}/messages?scope=private&id=123") as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(len((await response.json())["messages"]), 2)
            finally:
                if consumer is not None:
                    if consumer.poll() is None:
                        consumer.kill()
                    await asyncio.to_thread(consumer.wait, 5)
                if ingress.poll() is None:
                    ingress.kill()
                await asyncio.to_thread(ingress.wait, 5)
                output.close()
                await repository.close()


if __name__ == "__main__":
    try:
        program = unittest.main(verbosity=2, exit=False)
    finally:
        logging.shutdown()
        TEST_DATA.cleanup()
    sys.exit(0 if program.result.wasSuccessful() else 1)
