"""Startup workflow regressions with synthetic steps; no real programs/accounts."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from workspace_layout import SOURCE, test_root
sys.path.insert(0, str(SOURCE))
from startup_workflow import Chain, Outcome, Step, Workflow, register_startup, load_workflow
from startup_plan import wait_pi, wait_snowluma, pi_connected, snow_connected
from aiohttp import web


class Context:
    def clean(self, text):
        return text.replace("synthetic-secret", "[hidden]")


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=test_root(), prefix="startup-")
        self.root = Path(self.temp.name)
        self.workflows = []
        self.calls = []

    async def asyncTearDown(self):
        for workflow in self.workflows:
            if not workflow.closed:
                await workflow.close()
        self.temp.cleanup()

    def workflow(self, *chains):
        workflow = Workflow(chains, Context(), self.root / "settings.json", self.root / "state.json")
        self.workflows.append(workflow)
        return workflow

    def done(self, name):
        async def run(ctx):
            self.calls.append(name)
            return Outcome()
        return run

    async def until(self, condition):
        for _ in range(200):
            if condition():
                return
            await asyncio.sleep(.005)
        self.fail("Workflow did not reach the expected state")

    async def test_independent_failure_retry_resumes_only_failed_step(self):
        fixed = False
        async def pi(ctx):
            self.calls.append("pi-connect")
            if not fixed:
                raise RuntimeError("synthetic-secret unavailable")
            return Outcome()
        workflow = self.workflow(
            Chain("core", "Core", (Step("run", "Run", self.done("core")),), required=True),
            Chain("snow", "QQ", (Step("run", "Run", self.done("qq")),), dependencies=("core",)),
            Chain("pi", "Pi", (Step("prepare", "Prepare", self.done("pi-prepare")), Step("connect", "Connect", pi)), dependencies=("core",)))
        await workflow.command("start")
        await self.until(lambda: not workflow.tasks)
        self.assertEqual(workflow.chains["snow"].state, "done")
        self.assertEqual(workflow.chains["pi"].state, "failed")
        self.assertNotIn("synthetic-secret", (self.root / "state.json").read_text(encoding="utf-8"))
        fixed = True
        await workflow.command("continue", "pi")
        await self.until(lambda: not workflow.tasks)
        self.assertEqual(workflow.chains["pi"].state, "done")
        self.assertEqual(self.calls.count("core"), 1)
        self.assertEqual(self.calls.count("qq"), 1)
        self.assertEqual(self.calls.count("pi-prepare"), 1)
        self.assertEqual(self.calls.count("pi-connect"), 2)

    async def test_waiting_autocontinues_without_replaying_launch(self):
        ready = False
        async def wait(ctx):
            return Outcome.waiting("Please log in")
        async def probe(ctx):
            return ready
        workflow = self.workflow(Chain("snow", "QQ", (
            Step("launch", "Launch", self.done("launch")),
            Step("login", "Login", wait, probe, poll_interval=.01),
            Step("finish", "Finish", self.done("finish")))))
        await workflow.command("start")
        await self.until(lambda: workflow.chains["snow"].state == "waiting")
        await workflow.command("start")  # Duplicate clicks do not duplicate processes.
        self.assertEqual(self.calls, ["launch"])
        ready = True
        await self.until(lambda: workflow.chains["snow"].state == "done")
        self.assertEqual(self.calls, ["launch", "finish"])

    async def test_manual_continue_rechecks_and_never_assumes_success(self):
        ready = False
        async def wait(ctx):
            return Outcome() if ready else Outcome.waiting("Log in")
        workflow = self.workflow(Chain("pi", "Pi", (Step("wait", "Wait", wait),)))
        await workflow.command("start")
        await self.until(lambda: not workflow.tasks)
        await workflow.command("continue", "pi")
        await self.until(lambda: not workflow.tasks)
        self.assertEqual(workflow.chains["pi"].state, "waiting")
        ready = True
        await workflow.command("continue", "pi")
        await self.until(lambda: not workflow.tasks)
        self.assertEqual(workflow.chains["pi"].state, "done")

    async def test_skip_running_chain_never_executes_its_next_step(self):
        async def wait(ctx):
            await asyncio.Event().wait()
        workflow = self.workflow(
            Chain("pi", "Pi", (Step("wait", "Wait", wait), Step("later", "Later", self.done("forbidden")))),
            Chain("snow", "QQ", (Step("run", "Run", self.done("qq")),)))
        await workflow.command("start")
        await self.until(lambda: "qq" in self.calls)
        await workflow.command("skip", "pi")
        self.assertEqual(workflow.chains["pi"].state, "skipped")
        self.assertNotIn("forbidden", self.calls)
        self.assertEqual(workflow.chains["snow"].state, "done")

    async def test_required_failure_blocks_children_and_can_be_repaired(self):
        fixed = False
        async def core(ctx):
            if not fixed:
                raise RuntimeError("offline")
            return Outcome()
        workflow = self.workflow(
            Chain("core", "Core", (Step("run", "Run", core),), required=True),
            Chain("pi", "Pi", (Step("run", "Run", self.done("pi")),), dependencies=("core",)))
        await workflow.command("start")
        await self.until(lambda: not workflow.tasks)
        self.assertEqual(workflow.chains["pi"].state, "blocked")
        with self.assertRaises(ValueError):
            await workflow.command("skip", "core")
        fixed = True
        await workflow.command("continue", "pi")
        await self.until(lambda: not workflow.tasks)
        self.assertEqual(self.calls, ["pi"])

    async def test_cancel_pauses_scheduling_and_enabled_choices_persist(self):
        async def wait(ctx):
            await asyncio.Event().wait()
        core = Chain("core", "Core", (Step("run", "Run", wait),), required=True)
        pi = Chain("pi", "Pi", (Step("run", "Run", self.done("pi")),), dependencies=("core",))
        workflow = self.workflow(core, pi)
        await workflow.command("enable", "pi", False)
        fresh = self.workflow(core, pi)
        self.assertFalse(fresh.chains["pi"].enabled)
        await workflow.command("start")
        await workflow.command("cancel")
        self.assertFalse(workflow.tasks)
        self.assertTrue(workflow.paused)
        self.assertNotIn("pi", self.calls)

    async def test_readiness_checks_need_platform_connection_not_process_only(self):
        status = {"onebot_enabled": True, "onebot_connected": False,
                  "bridge": {"enabled": True, "ready": False}}
        async def query():
            return status
        ctx = types.SimpleNamespace(ingress_status=query)
        self.assertEqual((await wait_snowluma(ctx)).state, "waiting")
        self.assertEqual((await wait_pi(ctx)).state, "waiting")
        status["onebot_connected"] = True
        status["bridge"]["ready"] = True
        self.assertTrue(await snow_connected(ctx))
        self.assertTrue(await pi_connected(ctx))

    async def test_invalid_script_keeps_management_available_and_reload_can_repair_it(self):
        import paths
        source = self.root / "startup_plan.py"
        source.write_text("invalid python !!!", encoding="utf-8")
        with patch.object(paths, "code_root", return_value=self.root), \
                patch.object(paths, "data_root", return_value=self.root / "data"), \
                patch.object(paths, "log_root", return_value=self.root / "logs"):
            workflow = load_workflow(types.SimpleNamespace(), tolerate_errors=True)
            self.workflows.append(workflow)
            self.assertIn("configuration", workflow.chains)
            with self.assertRaises(SyntaxError):
                load_workflow(types.SimpleNamespace())

    async def test_source_reload_does_not_launch_anything(self):
        old = self.workflow(Chain("pi", "Old", (Step("run", "Run", self.done("old")),)))
        new = self.workflow(Chain("pi", "New", (Step("run", "Run", self.done("new")),)))
        app = web.Application()
        service = types.SimpleNamespace()
        register_startup(app, old, lambda req: True, service)
        action = next(r.handler for r in app.router.routes() if r.method == "POST")
        request = types.SimpleNamespace(match_info={"action": "reload"}, json=AsyncMock(return_value={}))
        with patch("startup_workflow.load_workflow", return_value=new):
            response = await action(request)
        self.assertEqual(response.status, 200)
        self.assertIs(service.startup_workflow, new)
        self.assertEqual(self.calls, [])

    async def test_api_auth_and_no_autostart_on_status(self):
        workflow = self.workflow(Chain("pi", "Pi", (Step("run", "Run", self.done("pi")),)))
        app = web.Application()
        service = types.SimpleNamespace(controllers={}, update_guard=None)
        register_startup(app, workflow, lambda req: req.headers.get("X-Manager-Token") == "synthetic", service)
        status_handler = next(r.handler for r in app.router.routes() if r.method == "GET")
        action_handler = next(r.handler for r in app.router.routes() if r.method == "POST")
        request = types.SimpleNamespace(headers={}, match_info={"action": "start"}, json=AsyncMock(return_value={"chain": "pi"}))
        self.assertEqual((await action_handler(request)).status, 401)
        request.headers = {"X-Manager-Token": "synthetic"}
        state = json.loads((await status_handler(request)).text)
        self.assertEqual(state["state"], "idle")
        self.assertEqual(self.calls, [])
        result = await action_handler(request)
        self.assertEqual(result.status, 200)
        await self.until(lambda: not workflow.tasks)
        self.assertEqual(self.calls, ["pi"])


if __name__ == "__main__":
    unittest.main()
