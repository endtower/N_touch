#!/usr/bin/env python3
"""Isolated acceptance for manual management; no real processes or user data.

Run: python scripts/acceptance_controller_race.py
"""
from __future__ import annotations

import concurrent.futures
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import manager_core
from core import supervisor


class FakeProc:
    def __init__(self, pid):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('Timed out waiting for test condition')


class ManualManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='abyss_manual_acceptance_')
        self.root = Path(self.temporary.name)
        self.specs = {
            name: supervisor.ProcSpec(
                name=name, cmd=['simulated'], cwd=self.root / name,
                out_log=self.root / f'{name}.log', ports=[62001 + index],
                health_url=f'http://test.invalid/{name}/health', order=index,
            ) for index, name in enumerate(('core', 'snowluma'))
        }
        self.created = []
        self.stopped = []
        self.healthy = threading.Event()
        self.healthy.set()
        self.spawned = threading.Event()
        self.stop_succeeds = True
        self.port_open = False
        self.patchers = [
            patch.object(manager_core, 'build_specs_safe', return_value=(self.specs, [])),
            patch.object(manager_core, 'spawn', side_effect=self.spawn),
            patch.object(manager_core, 'stop_tree', side_effect=self.stop),
            patch.object(manager_core, '_port_alive', side_effect=lambda port: self.port_open),
            patch.object(manager_core, '_health_ok', side_effect=lambda url: self.healthy.is_set()),
            patch.object(manager_core, 'HEALTH_WAIT_SEC', 2),
            patch.object(manager_core, 'GRACEFUL_STOP_TIMEOUT', 0),
            patch.object(manager_core, 'data_root', return_value=self.root),
            patch.object(manager_core, 'log_root', return_value=self.root / 'logs'),
        ]
        for patcher in self.patchers:
            patcher.start()
        # Choose when workers start, without constructing production data paths.
        with patch.object(threading.Thread, 'start'):
            self.service = manager_core.SupervisorService()
        self.controller = self.service.controller('core')

    def tearDown(self):
        for controller in self.service.controllers.values():
            controller._stop_event.set()
        for controller in self.service.controllers.values():
            if controller._worker.ident is not None:
                controller._worker.join(3)
                self.assertFalse(controller._worker.is_alive())
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary.cleanup()

    def spawn(self, spec, **kwargs):
        proc = FakeProc(900000 + len(self.created))
        self.created.append((spec.name, proc))
        self.spawned.set()
        return proc

    def stop(self, spec, pid):
        self.stopped.append(pid)
        if self.stop_succeeds:
            for name, proc in self.created:
                if proc.pid == pid:
                    proc.returncode = 0
        return self.stop_succeeds, pid

    def start_worker(self, name='core'):
        self.service.controller(name)._worker.start()

    def test_ten_starts_queued_before_worker_create_one_process(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(self.controller.submit, 'start') for _ in range(10)]
            wait_until(lambda: self.controller.queue.qsize() == 10)
            self.start_worker()
            self.assertTrue(all(future.result(3)['ok'] for future in futures))
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.stopped, [])

    def test_failed_stop_with_stale_ready_recovers_existing_process(self):
        controller = self.controller
        controller.proc = FakeProc(900123)
        controller.state, controller.ready, controller.managed = 'failed', True, True
        controller.desired, controller.last_error = 'stopped', 'stop failed'
        controller._check_exit()
        self.assertEqual((controller.state, controller.ready), ('running', True))
        self.assertEqual((controller.desired, controller.last_error), ('stopped', 'stop failed'))
        self.assertEqual(controller.snapshot().pid, 900123)
        self.assertEqual(self.created, [])
        self.assertEqual(self.stopped, [])

    def test_starting_process_becomes_ready_without_another_start(self):
        controller = self.controller
        controller.proc = FakeProc(900123)
        controller.state, controller.ready, controller.desired = 'starting', False, 'running'
        controller._check_exit()
        self.assertEqual((controller.state, controller.ready), ('running', True))
        self.assertEqual(self.created, [])

    def test_unhealthy_failed_process_is_not_reported_ready(self):
        self.healthy.clear()
        controller = self.controller
        controller.proc = FakeProc(900123)
        controller.state, controller.ready = 'failed', True
        controller._check_exit()
        self.assertEqual(controller.state, 'failed')
        self.assertFalse(controller.snapshot().ready)

    def test_exited_process_snapshot_never_claims_ready(self):
        controller = self.controller
        controller.proc = FakeProc(900123)
        controller.state, controller.ready = 'running', True
        controller.proc.returncode = 7
        snapshot = controller.snapshot()
        self.assertIsNone(snapshot.pid)
        self.assertFalse(snapshot.ready)

    def test_duplicate_starts_during_health_wait_do_not_cancel(self):
        self.healthy.clear()
        self.start_worker()
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            first = executor.submit(self.controller.submit, 'start')
            self.assertTrue(self.spawned.wait(1))
            others = [executor.submit(self.controller.submit, 'start') for _ in range(9)]
            wait_until(lambda: self.controller.queue.qsize() == 9)
            self.healthy.set()
            self.assertTrue(all(future.result(3)['ok'] for future in [first, *others]))
        self.assertEqual(len(self.created), 1)
        self.assertEqual(self.stopped, [])

    def test_release_startup_budget_waits_past_the_generic_limit(self):
        self.healthy.clear()
        self.specs['core'].startup_timeout = 0.8
        timer = threading.Timer(0.15, self.healthy.set)
        with patch.object(manager_core, 'HEALTH_WAIT_SEC', 0.05):
            self.start_worker()
            timer.start()
            try:
                self.assertTrue(self.controller.submit('start')['ok'])
            finally:
                timer.cancel(); timer.join()
        self.assertTrue(self.controller.ready)

    def test_crash_reports_failure_until_manual_start(self):
        self.start_worker()
        self.assertTrue(self.controller.submit('start')['ok'])
        self.controller.proc.returncode = 7
        wait_until(lambda: self.controller.state == 'failed')
        time.sleep(1.1)
        self.assertEqual(len(self.created), 1)
        self.assertIsNone(self.controller.proc)
        self.assertTrue(self.controller._worker.is_alive())
        self.assertTrue(self.controller.submit('start')['ok'])
        self.assertEqual(len(self.created), 2)

    def test_stop_core_does_not_stop_snowluma_or_respawn(self):
        self.start_worker()
        self.start_worker('snowluma')
        self.assertTrue(self.controller.submit('start')['ok'])
        snow = self.service.controller('snowluma')
        self.assertTrue(snow.submit('start')['ok'])
        core_pid, snow_proc = self.controller.proc.pid, snow.proc
        self.assertTrue(self.controller.submit('stop')['ok'])
        time.sleep(1.1)
        self.assertEqual(self.stopped, [core_pid])
        self.assertIsNone(snow_proc.poll())
        self.assertEqual(len(self.created), 2)
        self.assertEqual(self.controller.state, 'stopped')

    def test_cancelled_start_preserves_handle_if_stop_fails(self):
        self.healthy.clear()
        self.stop_succeeds = False
        self.start_worker()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.controller.submit, 'start')
            self.assertTrue(self.spawned.wait(1))
            wait_until(lambda: self.controller.proc is not None)
            original = self.controller.proc
            stopping = executor.submit(self.controller.submit, 'stop')
            self.assertFalse(first.result(3)['ok'])
            self.assertFalse(stopping.result(3)['ok'])
        self.assertIs(self.controller.proc, original)
        self.assertIsNone(original.poll())
        self.assertTrue(self.controller.managed)
        self.assertEqual(self.controller.state, 'failed')

    def test_restart_followed_by_stop_does_not_spawn(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            restart = executor.submit(self.controller.submit, 'restart')
            wait_until(lambda: self.controller.queue.qsize() == 1)
            stopping = executor.submit(self.controller.submit, 'stop')
            wait_until(lambda: self.controller.queue.qsize() == 2)
            self.start_worker()
            self.assertEqual(restart.result(3)['error_code'], 'superseded')
            self.assertTrue(stopping.result(3)['ok'])
        self.assertEqual(self.created, [])

    def test_manual_restart_creates_a_new_process(self):
        self.start_worker()
        self.assertTrue(self.controller.submit('start')['ok'])
        original_pid = self.controller.proc.pid
        self.assertTrue(self.controller.submit('restart')['ok'])
        self.assertEqual(self.stopped, [original_pid])
        self.assertNotEqual(self.controller.proc.pid, original_pid)
        self.assertEqual(len(self.created), 2)

    def test_external_port_is_never_taken_over_or_killed(self):
        self.port_open = True
        self.start_worker()
        self.assertEqual(self.controller.submit('start')['error_code'], 'conflict')
        self.assertEqual(self.controller.submit('stop')['error_code'], 'external_port')
        self.assertEqual(self.created, [])
        self.assertEqual(self.stopped, [])

    def test_old_disk_intent_is_ignored_and_no_marker_is_written(self):
        marker = self.root / 'logs' / 'desire' / 'core.txt'
        marker.parent.mkdir(parents=True)
        marker.write_text('running', encoding='utf-8')
        with patch.object(threading.Thread, 'start'):
            fresh_service = manager_core.SupervisorService()
        self.assertEqual(fresh_service.controller('core').desired, 'stopped')
        self.start_worker()
        self.assertTrue(self.controller.submit('start')['ok'])
        self.assertTrue(self.controller.submit('stop')['ok'])
        self.assertEqual(marker.read_text(), 'running')
        self.assertEqual(list(marker.parent.iterdir()), [marker])

    def test_running_process_keeps_its_original_spec(self):
        self.start_worker()
        self.assertTrue(self.controller.submit('start')['ok'])
        original_spec = self.controller.spec
        different = supervisor.ProcSpec(name='core', cmd=['new'], cwd=self.root,
            out_log=self.root / 'new.log', ports=[62099])
        self.specs['core'] = different
        self.assertTrue(self.controller.submit('start')['ok'])
        self.assertIs(self.controller.spec, original_spec)

    def test_graceful_stop_sends_local_service_token(self):
        self.start_worker()
        self.assertTrue(self.controller.submit('start')['ok'])
        token_file = self.root / 'manager' / 'service_token.txt'
        token_file.parent.mkdir()
        token_file.write_text('isolated-test-token', encoding='utf-8')
        proc = self.controller.proc
        class Response:
            def __enter__(self):
                proc.returncode = 0
                return self
            def __exit__(self, *args):
                return False
        with patch.object(manager_core.urllib.request, 'urlopen', return_value=Response()) as open_url:
            self.assertTrue(self.controller.submit('stop')['ok'])
            request = open_url.call_args.args[0]
            self.assertEqual(request.get_header('X-manager-token'), 'isolated-test-token')
            self.assertTrue(request.full_url.endswith('/system/core/shutdown-internal'))
        self.assertEqual(self.stopped, [])


class ProcessPrimitiveTests(unittest.TestCase):
    def test_unknown_pid_never_triggers_a_process_command(self):
        with patch.object(supervisor.subprocess, 'run') as run:
            self.assertEqual(supervisor.stop_tree(None, None), (False, None))
            run.assert_not_called()

    def test_spawn_preserves_hidden_window_and_log_redirection(self):
        with tempfile.TemporaryDirectory(prefix='abyss_spawn_acceptance_') as temporary:
            root = Path(temporary)
            spec = supervisor.ProcSpec(name='core', cmd=['simulated'], cwd=root,
                out_log=root / 'output.log', ports=[62000])
            config_package = types.ModuleType('config')
            env_tools = types.ModuleType('config.env_tools')
            env_tools.CONFIG_OVERRIDE_KEYS = ('WEB_WS_PORT',)
            with patch.dict(sys.modules, {'config': config_package, 'config.env_tools': env_tools}), \
                    patch.object(supervisor.subprocess, 'Popen') as popen:
                supervisor.spawn(spec, clean_env=True)
                kwargs = popen.call_args.kwargs
                self.assertTrue(kwargs['creationflags'] & supervisor.CREATE_NO_WINDOW)
                self.assertNotIn('WEB_WS_PORT', kwargs['env'])
                self.assertEqual(kwargs['stderr'], supervisor.subprocess.STDOUT)
                self.assertTrue(kwargs['stdout'].closed)

    def test_stop_uses_only_explicit_pid_and_hides_helper(self):
        with patch.object(supervisor.subprocess, 'run') as run, \
                patch.object(supervisor, 'wait_ports_free', return_value=True):
            self.assertEqual(supervisor.stop_tree(None, 900001), (True, 900001))
            self.assertEqual(run.call_args.args[0], ['taskkill', '/PID', '900001', '/T', '/F'])
            self.assertTrue(run.call_args.kwargs['creationflags'] & supervisor.CREATE_NO_WINDOW)


if __name__ == '__main__':
    unittest.main(verbosity=2)
