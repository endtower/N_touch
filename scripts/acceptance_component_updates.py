#!/usr/bin/env python3
"""Isolated update feed/package/control acceptance; never uses real user data."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, ClientTimeout, web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import manager_updates as updates_module
from manager_updates import Updates, register_updates, unpack, write_json
from package_component import package
from update_policy import allowed_file, version_key
from core import supervisor


async def confirmed_download(updates, version=None):
    review = await updates.review_core("download", version)
    return await updates.download("core", version, review["confirmation_token"])


async def confirmed_apply(updates, **kwargs):
    review = await updates.review_core("install", kwargs.get("version"))
    return await updates.apply("core", confirmation_token=review["confirmation_token"], **kwargs)


class Control:
    def __init__(self, name, service):
        self.name, self.service = name, service
        self.desired = "running"
        self.spec = types.SimpleNamespace(order={"ingress": 0, "snowluma": 1, "core": 2}[name])

    def snapshot(self):
        return types.SimpleNamespace(desired=self.desired)

    def submit(self, action, update_internal=False):
        if not update_internal and self.service.update_guard and self.service.update_guard(self.name):
            return {"ok": False, "error_code": "updating"}
        self.service.calls.append((self.name, action))
        self.desired = "running" if action == "start" else "stopped"
        return {"ok": True}


class Service:
    def __init__(self):
        self.epoch_lock = threading.Lock()
        self.update_guard = None
        self.calls = []
        self.controllers = {name: Control(name, self) for name in ("core", "snowluma", "ingress")}

    def controller(self, name):
        return self.controllers.get(name)


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        (ROOT / "tmp").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="component-updates-", dir=ROOT / "tmp")
        self.root = Path(self.temp.name)
        self.program, self.data = self.root / "program", self.root / "data"
        self.program.mkdir(); self.data.mkdir()
        self.patchers = [patch.object(updates_module, "app_root", return_value=self.program),
                         patch.object(updates_module, "data_root", return_value=self.data),
                         patch.object(updates_module, "resource_root", return_value=self.program),
                         patch.object(updates_module, "is_frozen", return_value=True)]
        for patcher in self.patchers:
            patcher.start()
        write_json(self.program / "component_versions.json", {"core": "0.1.0", "ingress": "0.1.0", "manager": "0.6.0", "launcher": "0.4.0"})
        self.service = Service()
        self.updates = Updates(self.service, "isolated-token", "http://127.0.0.1:1")
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "core.exe").write_bytes(b"new-core")
        (self.source / "core_internal").mkdir()
        (self.source / "core_internal" / "dependency.dll").write_bytes(b"new-runtime")
        self.archive = package("core", "0.2.0", self.source, self.root / "packages")
        feed_app = web.Application()
        feed_app.router.add_static("/", self.root / "packages")
        self.feed_runner = web.AppRunner(feed_app)
        await self.feed_runner.setup()
        site = web.TCPSite(self.feed_runner, "127.0.0.1", 0)
        await site.start()
        self.feed_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        self.manager_app = web.Application()
        register_updates(self.manager_app, self.updates, lambda request: request.headers.get("X-Manager-Token") == "isolated-token")
        self.manager_runner = web.AppRunner(self.manager_app)
        await self.manager_runner.setup()
        site = web.TCPSite(self.manager_runner, "127.0.0.1", 0)
        await site.start()
        self.manager_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"

    async def asyncTearDown(self):
        await self.manager_runner.cleanup(); await self.feed_runner.cleanup()
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    async def test_online_check_download_and_restart_preserves_staged_package(self):
        write_json(self.updates.config_path, {"sources": {"core": self.feed_url + "/core-latest.json", "snowluma": ""}})
        await self.updates.check("core")
        self.assertEqual(self.updates.rows["core"]["state"], "available")
        await confirmed_download(self.updates)
        self.assertEqual((self.updates.root / "core/payload/core.exe").read_bytes(), b"new-core")
        fresh = Updates(self.service, "isolated-token", self.manager_url)
        row = next(row for row in fresh.status()["components"] if row["component"] == "core")
        self.assertEqual((row["state"], row["latest"]), ("ready", "0.2.0"))
        self.assertEqual(self.service.calls, [])

    async def test_hash_mismatch_never_creates_ready_package(self):
        write_json(self.updates.config_path, {"sources": {"core": self.feed_url + "/core-latest.json"}})
        await self.updates.check("core")
        # 发布清单本身声明错误摘要；确认应绑定这个清单，下载仍须拒绝内容不匹配。
        for release in self.updates.versions("core").values():
            release["sha256"] = "0" * 64
        catalog = updates_module.read_json(self.updates.root / "catalog/core.json")
        for release in catalog["releases"]:
            release["sha256"] = "0" * 64
        updates_module.write_json(self.updates.root / "catalog/core.json", catalog)
        self.updates.rows["core"]["release"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA256"):
            await confirmed_download(self.updates)
        self.assertFalse((self.updates.root / "core/ready.json").exists())

    async def test_select_older_published_version_and_reinstall_current(self):
        old = package("core", "0.0.9", self.source, self.root / "packages")
        write_json(self.updates.config_path, {"sources": {"core": self.feed_url + "/core-latest.json", "snowluma": ""}})
        # Publish the current release after the older one; the catalog retains both.
        package("core", "0.2.0", self.source, self.root / "packages")
        await self.updates.check("core", history=True)
        self.assertEqual(list(self.updates.versions("core")), ["0.2.0", "0.0.9"])
        await confirmed_download(self.updates, "0.0.9")
        self.assertEqual(updates_module.read_json(self.updates.root / "core/ready.json")["version"], "0.0.9")
        self.assertEqual(self.service.calls, [])
        fresh = Updates(self.service, "isolated-token", self.manager_url)
        self.assertIn("0.0.9", fresh.versions("core"))
        with patch.object(fresh, "fetch_json", side_effect=AssertionError("Local rollback must work offline")):
            await confirmed_download(fresh, "0.0.9")
        # A package for the installed version is also selectable for repair/reinstall.
        current = package("core", "0.1.0", self.source, self.root / "current-packages")
        await fresh.import_local("core", str(current))
        await confirmed_download(fresh, "0.1.0")
        self.assertEqual(updates_module.read_json(fresh.root / "core/ready.json")["version"], "0.1.0")

    async def test_previous_install_backup_is_selectable_and_verified(self):
        job = self.updates.root / "jobs/old-install"
        (job / "backup/core_internal").mkdir(parents=True)
        (job / "backup/core.exe").write_bytes(b"old-core")
        (job / "backup/core_internal/runtime.dll").write_bytes(b"old-runtime")
        files = {name: self.updates.hash_file(job / "backup" / name) for name in ("core.exe", "core_internal/runtime.dll")}
        write_json(job / "job.json", {"component": "core"})
        write_json(job / "journal.json", {"state": "installed"})
        write_json(job / "previous-receipt.json", {"version": "0.0.8", "target_root": str(self.program.resolve()), "files": files})
        self.assertIn("0.0.8", self.updates.versions("core"))
        await confirmed_download(self.updates, "0.0.8")
        self.assertEqual((self.updates.root / "core/payload/core.exe").read_bytes(), b"old-core")
        self.assertEqual(self.service.calls, [])
        # Corruption must fail before replacing the installed program.
        (job / "backup/core.exe").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.updates.stage_backup("core", {"backup": str(job), "version": "0.0.8"})

    async def test_apply_rejects_selected_version_different_from_staged(self):
        await self.updates.import_local("core", str(self.archive))
        with self.assertRaisesRegex(ValueError, "所选版本不同"):
            await confirmed_apply(self.updates, version="0.0.9")
        self.assertIsNone(self.updates.active())
        self.assertEqual(self.service.calls, [])

    async def test_official_snow_history_and_explicit_older_tag(self):
        def release(version):
            return {"tag_name": "v" + version, "assets": [{"name": f"SnowLuma-v{version}-win-x64.zip",
                     "digest": "sha256:" + "a" * 64, "size": 123, "browser_download_url": f"https://example.org/{version}.zip"}]}
        write_json(self.updates.config_path, {"sources": {"snowluma": updates_module.SNOW_FEED}})
        with patch.object(self.updates, "fetch_json", side_effect=[release("1.14.22"), [release("1.14.22"), release("1.12.1"), {"draft": True}]]):
            await self.updates.check("snowluma", history=True)
        self.assertEqual(list(self.updates.versions("snowluma")), ["1.14.22", "1.12.1"])
        # The editable selector can request a tag older than the first 100 releases.
        with patch.object(self.updates, "fetch_json", return_value=release("1.10.0")) as fetch, \
                patch.object(updates_module.aiohttp, "ClientSession", side_effect=ValueError("stop-before-download")):
            with self.assertRaisesRegex(ValueError, "stop-before-download"):
                await self.updates.download("snowluma", "1.10.0")
        fetch.assert_awaited_once_with(updates_module.SNOW_RELEASES + "/tags/v1.10.0")

    async def test_api_auth_and_unknown_component(self):
        async with ClientSession(timeout=ClientTimeout(total=5)) as client:
            async with client.get(self.manager_url + "/v1/updates") as response:
                self.assertEqual(response.status, 401)
            async with client.post(self.manager_url + "/v1/updates/check", json={"component": "unknown"},
                                   headers={"X-Manager-Token": "isolated-token"}) as response:
                self.assertEqual(response.status, 400)

    async def test_component_failure_does_not_block_other_checks(self):
        write_json(self.updates.config_path, {"sources": {"core": self.feed_url + "/core-latest.json",
                                                        "snowluma": self.feed_url + "/missing.json"}})
        await self.updates.check()
        self.assertEqual(self.updates.rows["core"]["state"], "available")
        self.assertEqual(self.updates.rows["snowluma"]["state"], "error")

    async def test_apply_stops_only_core_and_locks_its_commands(self):
        await self.updates.import_local("core", str(self.archive))
        (self.program / "updater").mkdir()
        (self.program / "updater/AbyssUpdater.exe").write_bytes(b"fake-helper")
        (self.program / "core.exe").write_bytes(b"old-core")
        with patch.object(updates_module.subprocess, "Popen", return_value=types.SimpleNamespace(pid=123456)):
            result = await confirmed_apply(self.updates)
        self.assertTrue(result["ok"])
        self.assertEqual(self.service.calls, [("core", "stop")])
        self.assertEqual(self.service.controller("core").submit("start")["error_code"], "updating")
        self.assertTrue(self.service.controller("snowluma").submit("start")["ok"])
        job = updates_module.read_json(Path(self.updates.active()["job"]))
        self.assertEqual(job["previous_files"], ["core.exe"])
        write_json(self.updates.root / "core/result.json", {"id": result["job"], "state": "installed", "message": "done"})
        await self.updates.finish(result["job"])
        self.assertEqual(self.service.calls[-1], ("core", "start"))
        self.assertIsNone(self.updates.active())

    async def test_source_mode_cannot_apply_binary_updates(self):
        with patch.object(updates_module, "is_frozen", return_value=False):
            with self.assertRaisesRegex(ValueError, "源码"):
                await self.updates.apply("core")

    async def test_manager_update_does_not_stop_unowned_targets(self):
        source = self.root / "manager-source"
        (source / "manager_internal").mkdir(parents=True)
        (source / "manager.exe").write_bytes(b"manager")
        (source / "manager_internal/runtime.dll").write_bytes(b"runtime")
        archive = package("manager", "0.6.1", source, self.root / "manager-packages")
        await self.updates.import_local("manager", str(archive))
        (self.program / "updater").mkdir()
        (self.program / "updater/AbyssUpdater.exe").write_bytes(b"fake-helper")
        self.service.controller("snowluma").desired = "stopped"
        self.service.controller("core").desired = "stopped"
        with patch.object(updates_module.subprocess, "Popen", return_value=types.SimpleNamespace(pid=123456)), \
                patch.object(asyncio.get_running_loop(), "call_later") as exit_delay:
            await self.updates.apply("manager")
            exit_delay.assert_called_once()
        self.assertEqual(self.service.calls, [("ingress", "stop")])

    async def test_interrupted_update_has_explicit_recovery_state(self):
        write_json(self.updates.root / "active.json", {"component": "core", "id": "broken", "worker_pid": 0})
        row = next(row for row in self.updates.status()["components"] if row["component"] == "core")
        self.assertEqual(row["state"], "recovery_required")
        self.assertFalse(self.updates.blocked("snowluma"))

    async def test_archive_rejects_traversal_collision_and_user_data(self):
        for name in ("../secret", "core_internal/../secret", "config/settings.json", "memory/chat.db", "core_internal/NUL.dll"):
            bad = self.root / "bad.zip"
            with zipfile.ZipFile(bad, "w") as archive:
                archive.writestr(name, b"bad")
            with self.assertRaises(ValueError):
                unpack(bad, self.root / "staging", "core")
        with zipfile.ZipFile(bad, "w") as archive:
            archive.writestr("core.exe", b"one"); archive.writestr("CORE.EXE", b"two")
        with self.assertRaises(ValueError):
            unpack(bad, self.root / "staging", "core")

    async def test_snow_upstream_skips_config_and_data(self):
        archive = self.root / "snow.zip"
        with zipfile.ZipFile(archive, "w") as source:
            for name, content in {"index.mjs": "new", "node.exe": "runtime", "package.json": '{"version":"1.12.2"}',
                                  "config/runtime.json": "template", "data/login.db": "never-install",
                                  "client/index.html": "ui", "native/hook.dll": "hook",
                                  "release.json": '{"version":"1.12.2"}',
                                  "check-node-version.cjs": "runtime-check"}.items():
                source.writestr("SnowLuma/" + name, content)
        payload = self.root / "snow-payload"
        files = unpack(archive, payload, "snowluma", True)
        self.assertNotIn("config/runtime.json", files)
        self.assertFalse((payload / "data").exists())
        self.assertEqual((payload / "index.mjs").read_text(), "new")
        self.assertEqual((payload / "release.json").read_text(), '{"version":"1.12.2"}')
        self.assertEqual((payload / "check-node-version.cjs").read_text(), "runtime-check")

    async def test_packages_exclude_private_files_and_other_components(self):
        (self.source / ".env.dev").write_text("private-key")
        (self.source / "manager.exe").write_bytes(b"other-component")
        target = package("core", "0.2.1", self.source, self.root / "clean-packages")
        with zipfile.ZipFile(target) as archive:
            self.assertEqual(set(archive.namelist()), {"core.exe", "core_internal/dependency.dll"})

    async def test_release_process_paths_match_independent_installs(self):
        with patch.object(supervisor, "is_frozen", return_value=True), patch.object(supervisor, "app_root", return_value=self.program):
            self.assertEqual(supervisor.core_cmd(), [str(self.program / "core.exe")])
            self.assertEqual(supervisor.ingress_cmd(), [str(self.program / "ingress.exe")])

    async def test_semver_is_numeric_and_prereleases_are_ordered(self):
        self.assertGreater(version_key("1.10.0"), version_key("1.9.9"))
        self.assertGreater(version_key("1.0.0"), version_key("1.0.0-beta.10"))
        self.assertGreater(version_key("1.0.0-beta.10"), version_key("1.0.0-beta.2"))
        self.assertFalse(allowed_file("snowluma", "config/runtime.json"))
        self.assertFalse(allowed_file("launcher", "manager.exe"))

    async def test_core_never_downloads_without_explicit_confirmation(self):
        write_json(self.updates.config_path, {"sources": {"core": self.feed_url + "/core-latest.json"}})
        await self.updates.check("core")
        self.assertFalse((self.updates.root / "core/ready.json").exists())
        with self.assertRaisesRegex(ValueError, "确认"):
            await self.updates.download("core", "0.2.0")
        self.assertFalse((self.updates.root / "core/ready.json").exists())
        self.assertEqual(self.service.calls, [])

    async def test_core_install_requires_a_separate_confirmation(self):
        await self.updates.import_local("core", str(self.archive))
        with self.assertRaisesRegex(ValueError, "确认"):
            await self.updates.apply("core", version="0.2.0")
        self.assertIsNone(self.updates.active())
        self.assertEqual(self.service.calls, [])

    async def test_confirmation_is_single_use_and_expires(self):
        await self.updates.import_local("core", str(self.archive))
        review = await self.updates.review_core("download", "0.2.0")
        await self.updates.download("core", "0.2.0", review["confirmation_token"])
        with self.assertRaisesRegex(ValueError, "确认"):
            await self.updates.download("core", "0.2.0", review["confirmation_token"])
        review = await self.updates.review_core("download", "0.2.0")
        token = review["confirmation_token"]
        old = self.updates.confirmations[token]
        self.updates.confirmations[token] = (0, *old[1:])
        with self.assertRaisesRegex(ValueError, "过期"):
            await self.updates.download("core", "0.2.0", token)

    async def test_confirmed_files_changing_before_install_are_preserved(self):
        await self.updates.import_local("core", str(self.archive))
        (self.program / "core.exe").write_bytes(b"official")
        review = await self.updates.review_core("install", "0.2.0")
        (self.program / "core.exe").write_bytes(b"local-edit-after-confirmation")
        with self.assertRaisesRegex(ValueError, "已变化"):
            await self.updates.apply("core", version="0.2.0", confirmation_token=review["confirmation_token"])
        self.assertEqual((self.program / "core.exe").read_bytes(), b"local-edit-after-confirmation")
        self.assertEqual(self.service.calls, [])

    async def test_local_source_additions_and_edits_block_install(self):
        await self.updates.import_local("core", str(self.archive))
        file = self.program / "components/core/source/main.py"
        file.parent.mkdir(parents=True)
        file.write_text("official")
        write_json(self.updates.receipt_path("core"), {"version": "0.1.0", "target_root": str(self.program.resolve()),
                   "files": {"components/core/source/main.py": self.updates.hash_file(file)}})
        file.write_text("local edit")
        (file.parent / "custom.py").write_text("local addition")
        review = await self.updates.review_core("install", "0.2.0")
        self.assertFalse(review["can_proceed"])
        self.assertEqual(len(review["local_changes"]), 2)
        self.assertEqual(review["confirmation_token"], "")
        self.assertEqual(self.service.calls, [])

    async def test_core_release_risks_are_retained_and_compatibility_is_enforced(self):
        archive = package("core", "0.3.0", self.source, self.root / "risk-packages", metadata={
            "notes": "变更说明", "breaking_changes": ["不兼容旧接口"], "requires": {"manager": "99.0.0"},
            "data_policy": "migration", "migration_notes": "安装前备份；回退需人工恢复数据。"})
        await self.updates.import_local("core", str(archive))
        fresh = Updates(self.service, "isolated-token", self.manager_url)
        review = await fresh.review_core("install", "0.3.0")
        self.assertEqual(review["release"]["breaking_changes"], ["不兼容旧接口"])
        self.assertIn("人工恢复", review["release"]["migration_notes"])
        self.assertFalse(review["can_proceed"])
        self.assertIn("manager >= 99.0.0", review["blockers"][0])

    async def test_install_creates_backups_and_passes_confirmed_hashes_to_installer(self):
        await self.updates.import_local("core", str(self.archive))
        (self.program / "updater").mkdir()
        (self.program / "updater/AbyssUpdater.exe").write_bytes(b"fake-helper")
        (self.program / "core.exe").write_bytes(b"official")
        (self.data / "memory").mkdir()
        (self.data / "memory/memory.db").write_bytes(b"isolated-database-sentinel")
        review = await self.updates.review_core("install", "0.2.0")
        with patch.object(updates_module.subprocess, "Popen", return_value=types.SimpleNamespace(pid=123456)):
            await self.updates.apply("core", version="0.2.0", confirmation_token=review["confirmation_token"])
        job_path = Path(self.updates.active()["job"])
        job = updates_module.read_json(job_path)
        self.assertTrue(job["protect_local_changes"])
        self.assertEqual(job["expected_files"]["core.exe"], self.updates.hash_file(self.program / "core.exe"))
        self.assertEqual((job_path.parent / "data-backup/memory/memory.db").read_bytes(), b"isolated-database-sentinel")
        self.assertFalse(updates_module.read_json(job_path.parent / "data-backup/backup.json")["automatic_restore"])

    async def test_fresh_source_distribution_has_no_fake_installed_version(self):
        write_json(self.program / "update_defaults.json", {"core_distribution": "source-v1", "sources": {"core": self.feed_url + "/core-latest.json"}})
        self.assertEqual(self.updates.version("core"), "未安装")
        self.assertEqual(self.updates.version("ingress"), "未安装")
        self.assertEqual(self.updates.config()["sources"]["core"], self.feed_url + "/core-latest.json")
        row = next(x for x in self.updates.status()["components"] if x["component"] == "ingress")
        self.assertEqual(row["managed_by"], "core")
        self.assertFalse(row["installable"])

    async def test_snowluma_version_prefers_installed_package_over_stale_receipt(self):
        target = self.root / "SnowLuma"
        target.mkdir()
        write_json(target / "package.json", {"version": "2.0.3"})
        # 另一个 app_root（例如源码运行）留下的旧 receipt 指向同一目标目录。
        write_json(self.updates.receipt_path("snowluma"),
                   {"version": "1.14.22", "target_root": str(target), "files": {"index.mjs": "0" * 64}})
        with patch.object(supervisor, "snowluma_root", return_value=target):
            self.assertEqual(self.updates.version("snowluma"), "2.0.3")

    async def test_snowluma_stale_receipt_scans_current_files_before_update(self):
        target = self.root / "SnowLuma"
        target.mkdir()
        (target / "index.mjs").write_bytes(b"2.0.3")
        (target / "node.exe").write_bytes(b"node")
        (target / "client").mkdir()
        (target / "client" / "old-asset.js").write_bytes(b"old")
        write_json(target / "package.json", {"version": "2.0.3"})
        # receipt 只记录更早的 1.14.22，不能据此漏掉当前版本的文件。
        write_json(self.updates.receipt_path("snowluma"),
                   {"version": "1.14.22", "target_root": str(target), "files": {"index.mjs": "0" * 64}})
        source = self.root / "snow-source"
        source.mkdir()
        (source / "index.mjs").write_bytes(b"new")
        (source / "node.exe").write_bytes(b"node")
        (source / "client").mkdir()
        (source / "client" / "new-asset.js").write_bytes(b"new")
        write_json(source / "package.json", {"version": "2.1.0"})
        archive = package("snowluma", "2.1.0", source, self.root / "snow-packages")
        await self.updates.import_local("snowluma", str(archive))
        (self.program / "updater").mkdir(exist_ok=True)
        (self.program / "updater/AbyssUpdater.exe").write_bytes(b"fake-helper")
        with patch.object(supervisor, "snowluma_root", return_value=target), \
                patch.object(updates_module.subprocess, "Popen", return_value=types.SimpleNamespace(pid=123456)):
            await self.updates.apply("snowluma")
        job = updates_module.read_json(Path(self.updates.active()["job"]))
        self.assertIn("index.mjs", job["previous_files"])
        self.assertIn("client/old-asset.js", job["previous_files"])

    async def check_failed_startup(self, data_policy):
        folder = self.updates.root / "jobs/failed-startup"
        job_path = folder / "job.json"
        write_json(job_path, {"previous": ["ingress", "core"], "release": {"data_policy": data_policy}})
        write_json(self.updates.root / "active.json", {"id": "failed-startup", "component": "core",
                   "job": str(job_path), "worker_pid": 0, "previous": ["ingress", "core"], "targets": ["ingress", "core"]})
        write_json(self.updates.root / "core/result.json", {"id": "failed-startup", "state": "installed", "message": "installed"})
        core = self.service.controller("core")
        original = core.submit
        attempts = 0
        def start_once_failed(action, update_internal=False):
            nonlocal attempts
            if action == "start":
                attempts += 1
                if attempts == 1:
                    return {"ok": False, "message": "new Core unhealthy"}
            return original(action, update_internal)
        self.assertTrue(self.updates.blocked("ingress"))
        self.assertFalse(self.updates.blocked("snowluma"))
        with patch.object(core, "submit", side_effect=start_once_failed), patch.object(
                updates_module.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as rollback:
            result = await self.updates.finish("failed-startup")
        self.assertIn("--rollback", rollback.call_args.args[0])
        return result

    async def test_failed_compatible_core_rolls_back_program_and_restarts(self):
        result = await self.check_failed_startup("compatible")
        self.assertEqual(result["state"], "rolled_back")
        self.assertIsNone(self.updates.active())
        self.assertEqual(self.service.controller("core").desired, "running")

    async def test_failed_migration_stays_stopped_until_explicit_data_acknowledgment(self):
        result = await self.check_failed_startup("migration")
        self.assertEqual(result["state"], "recovery_required")
        self.assertTrue(result["data_restore_required"])
        self.assertEqual(self.service.controller("core").desired, "stopped")
        self.assertTrue(self.updates.blocked("ingress"))
        # 恢复说明仍需完整元数据，不能把一次安装确认复用成人工数据处理确认。
        job_path = Path(self.updates.active()["job"])
        job = updates_module.read_json(job_path)
        job["release"].update(migration_notes="人工核对并恢复数据备份后再继续")
        write_json(job_path, job)
        with self.assertRaisesRegex(ValueError, "确认"):
            await self.updates.recover()
        review = await self.updates.review_core("recover")
        result = await self.updates.recover(confirmation_token=review["confirmation_token"])
        self.assertEqual(result["state"], "rolled_back")
        self.assertIsNone(self.updates.active())
        self.assertEqual(self.service.controller("core").desired, "running")

    async def test_source_package_paths_and_user_data_boundaries(self):
        self.assertTrue(allowed_file("core", "components/core/source/brain/att/station.py"))
        self.assertTrue(allowed_file("core", "components/core/source/memory/message_repository.py"))
        self.assertTrue(allowed_file("core", "components/core/runtime/python.exe"))
        self.assertFalse(allowed_file("core", "components/core/source/.env.dev"))
        self.assertFalse(allowed_file("core", "components/core/source/memory/memory.db"))
        self.assertFalse(allowed_file("core", "components/core/source/brain/base/persona/common.json"))

    async def test_managed_source_processes_and_web_use_installed_release(self):
        from paths import web_dist
        bundle = self.program / "components/core"
        (bundle / "source/web_frontend/dist").mkdir(parents=True)
        write_json(bundle / "release.json", {"version": "0.4.0", "format": "source-v1"})
        with patch.object(supervisor, "is_frozen", return_value=True), patch.object(supervisor, "app_root", return_value=self.program), \
                patch.dict(updates_module.os.environ, {"ABYSS_RUN_MODE": ""}), patch("paths.app_root", return_value=self.program):
            self.assertEqual(supervisor.core_cmd(), [str(bundle / "runtime/python.exe"), "-u", str(bundle / "source/main.py")])
            self.assertEqual(supervisor.ingress_cmd(), [str(bundle / "runtime/python.exe"), "-u", str(bundle / "source/ingress_service.py")])
            self.assertEqual(web_dist(), bundle / "source/web_frontend/dist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
