#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manager_core —— 目标进程串行化控制器（独立管理服务的核心）

修复第二轮复核 P1 的两个竞态：
1. 并发 start：每个目标由唯一工作线程串行处理全部启停命令（存活检查与
   spawn 在同一临界区内），连点只产生一个实例；
2. 退避期停止不生效：崩溃恢复不再"锁外 sleep 后无条件 start"，而是登记
   待执行恢复（携带命令版本 epoch），工作循环每秒醒来先处理用户新命令，
   恢复到期执行前重新核对 desired 与 epoch——期间任何 stop/restart 都
   会使待执行恢复作废。

状态机（对外表达）：stopped / starting / running / stopping / failed /
conflict / running_untracked（外部实例，只探测不控制）。
就绪（业务健康）与存活分开表达：core 用 /health 探测。

所有权：只控制自己 spawn 的进程句柄；端口被外部程序占用时报告 conflict，
绝不按端口反查 PID 击杀无关进程（GPT 审查/复核一致要求）。
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from paths import app_root, data_root, log_root
from core.supervisor import (
    ProcSpec,
    build_specs,
    spawn,
    stop_tree,
)

STATES = ("stopped", "starting", "running", "stopping", "failed", "conflict")

HEALTH_WAIT_SEC = 30
GRACEFUL_STOP_TIMEOUT = 10
CRASH_LOOP_WINDOW = 10        # 启动后 N 秒内退出视为崩溃循环，不自动恢复
BACKOFF_INITIAL = 5
BACKOFF_MAX = 60


# ============================================================
# 进程规格：配置加载容错（Core 配置非法时管理服务必须活着）
# ============================================================

def build_specs_safe() -> tuple[dict[str, ProcSpec], list[str]]:
    """构建目标规格；Core 配置非法时降级为默认端口并记录错误，不让服务崩。"""
    errors: list[str] = []
    try:
        specs = build_specs()
        return specs, errors
    except Exception as e:
        errors.append(f"Core 配置加载失败（{type(e).__name__}: {e}），已用默认端口 5001/5002 降级")
    from core.supervisor import core_ports
    try:
        web_port, onebot_port = core_ports()
    except Exception:
        web_port, onebot_port = 5002, 5001
    from core.supervisor import SNOWLUMA_DIR, LOG_DIR
    specs = {
        "core": ProcSpec(
            name="core",
            cmd=[sys.executable, "-u", "main.py"],
            cwd=app_root(),
            out_log=LOG_DIR / "core.out.log",
            ports=[web_port, onebot_port],
            health_url=f"http://127.0.0.1:{web_port}/health",
            order=0,
            desc="Abyss Core（降级端口）",
        ),
        "snowluma": ProcSpec(
            name="snowluma",
            cmd=["cmd", "/c", "launcher.bat"],
            cwd=SNOWLUMA_DIR,
            out_log=LOG_DIR / "snowluma.out.log",
            ports=[5099, 3001],
            order=1,
            auto_restart=False,
            desc="SnowLuma（降级端口）",
        ),
    }
    return specs, errors


def _port_alive(port: int, timeout: float = 0.4) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _health_ok(url: str) -> bool:
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


# ============================================================
# 目标控制器
# ============================================================

@dataclass
class TargetSnapshot:
    name: str
    state: str
    desired: str
    pid: int | None
    ports: list
    ready: bool
    managed: bool
    restarts: int
    last_error: str
    uptime_sec: float | None


class TargetController:
    """单个目标的唯一控制者：专属工作线程串行处理全部启停命令与崩溃恢复。"""

    def __init__(self, spec: ProcSpec, service: "SupervisorService"):
        self.spec = spec
        self.service = service
        self.queue: queue.Queue = queue.Queue()
        self.state = "stopped"
        self.desired = self.service.read_desire(spec.name)
        self.ready = False            # 业务健康（core：/health 200）
        self.managed = False          # 当前进程是否为本控制器 spawn（所有权）
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.restarts = 0
        self.last_error = ""
        self.epoch = 0                # 用户命令版本号：待执行恢复据此作废
        self.pending_recovery: tuple[int, float] | None = None   # (epoch, run_at)
        self.backoff = BACKOFF_INITIAL
        self._stop_event = threading.Event()
        self._wake = threading.Event()
        threading.Thread(target=self._run, daemon=True, name=f"ctl-{spec.name}").start()

    # ---------------- 对外命令（线程安全，立即入队）----------------

    def submit(self, kind: str) -> dict:
        """提交 start/stop/restart 命令并等待执行结果（串行化保证）。"""
        if kind not in ("start", "stop", "restart"):
            return {"ok": False, "error_code": "bad_command", "message": f"未知命令 {kind}"}
        with self._lock_epoch():
            self.epoch += 1
            epoch = self.epoch
        done = threading.Event()
        result: dict = {}
        self.queue.put((kind, epoch, done, result))
        done.wait(timeout=HEALTH_WAIT_SEC + GRACEFUL_STOP_TIMEOUT + 15)
        if not result:
            return {"ok": False, "error_code": "timeout",
                    "message": "命令排队超时（目标正被其他操作占用），稍后重试"}
        return result

    def _lock_epoch(self):
        return self.service.epoch_lock

    def snapshot(self) -> TargetSnapshot:
        uptime = time.time() - self.started_at if (self.proc and self.proc.poll() is None
                                                   and self.started_at) else None
        pid = self.proc.pid if (self.proc and self.proc.poll() is None) else None
        return TargetSnapshot(
            name=self.spec.name, state=self.state, desired=self.desired,
            pid=pid, ports=list(self.spec.ports), ready=self.ready,
            managed=self.managed, restarts=self.restarts,
            last_error=self.last_error, uptime_sec=round(uptime, 1) if uptime else None,
        )

    # ---------------- 工作线程 ----------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            item = None
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                pass
            self._check_crash()
            self._check_recovery()
            if item is not None:
                kind, epoch, done, result = item
                try:
                    if kind == "start":
                        result.update(self._cmd_start(epoch))
                    elif kind == "stop":
                        result.update(self._cmd_stop(epoch))
                    elif kind == "restart":
                        r1 = self._cmd_stop(epoch)
                        result.update(r1)
                        if r1.get("ok", False) or r1.get("error_code") == "not_running":
                            r2 = self._cmd_start(epoch)
                            result.update(r2)
                except Exception as e:   # 防御：控制器线程绝不能死
                    result.update({"ok": False, "error_code": "internal",
                                   "message": f"{type(e).__name__}: {e}"})
                finally:
                    done.set()
                    self._wake.clear()

    # ---------------- 命令实现（仅工作线程调用）----------------

    def _ports_busy_foreign(self) -> bool:
        """端口开放但不是我们管理的进程 → 外部实例/冲突。"""
        return any(_port_alive(p) for p in self.spec.ports)

    def _cmd_start(self, epoch: int) -> dict:
        if self.proc is not None and self.proc.poll() is None:
            self.state = "starting" if not self.ready else "running"
            return {"ok": True, "state": self.state, "pid": self.proc.pid,
                    "message": "目标进程已在（本服务管理），不重复创建"}
        if self._ports_busy_foreign():
            self.state = "conflict"
            self.last_error = "端口被外部程序占用（不是本服务启动的实例），不自动接管"
            return {"ok": False, "error_code": "conflict", "state": self.state,
                    "message": self.last_error}
        self.desired = "running"
        self.service.write_desire(self.spec.name, "running")
        self.state = "starting"
        self.last_error = ""
        try:
            proc = spawn(self.spec, detached=False)
        except Exception as e:
            self.state = "failed"
            self.last_error = f"spawn 失败: {e}"
            return {"ok": False, "error_code": "spawn_failed", "state": self.state,
                    "message": self.last_error}
        self.proc = proc
        self.started_at = time.time()
        self.managed = True
        deadline = time.time() + HEALTH_WAIT_SEC
        while time.time() < deadline:
            if proc.poll() is not None:
                self.state = "failed"
                self.last_error = f"启动后立即退出 (exitcode={proc.returncode})"
                return {"ok": False, "error_code": "exited", "state": self.state,
                        "message": self.last_error}
            if _health_ok(self.spec.health_url) if self.spec.health_url \
                    else any(_port_alive(p) for p in self.spec.ports):
                self.state = "running"
                self.ready = True
                self.backoff = BACKOFF_INITIAL
                return {"ok": True, "state": "running", "pid": proc.pid,
                        "message": "已启动并就绪"}
            time.sleep(0.5)
        # 超时：进程保持追踪（不丢弃句柄，避免未追踪进程），报 failed 但可 stop
        self.state = "failed"
        self.ready = False
        self.last_error = f"{HEALTH_WAIT_SEC}s 内未就绪（进程仍在，可停止后重试）"
        return {"ok": False, "error_code": "not_ready", "state": self.state,
                "pid": proc.pid, "message": self.last_error}

    def _cmd_stop(self, epoch: int) -> dict:
        self.desired = "stopped"
        self.service.write_desire(self.spec.name, "stopped")
        self.pending_recovery = None      # 用户停止撤销一切待执行恢复
        self.state = "stopping"
        pid = self.proc.pid if (self.proc and self.proc.poll() is None) else None
        if pid is None:
            if any(_port_alive(p) for p in self.spec.ports):
                self.state = "conflict"
                self.last_error = "端口被外部程序占用；本轮不终止外部实例"
                return {"ok": False, "error_code": "conflict", "state": self.state,
                        "message": self.last_error}
            self.state = "stopped"
            self.ready = False
            self.managed = False
            return {"ok": True, "error_code": "not_running", "state": "stopped",
                    "message": "目标未在运行"}
        # 优雅优先：core 走自身 /system/core/shutdown；snowluma 无已知优雅通道
        if self.spec.name == "core" and _health_ok(self.spec.health_url):
            try:
                req = urllib.request.Request(
                    self.spec.health_url.replace("/health", "/system/core/shutdown"),
                    method="POST")
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass
        deadline = time.time() + GRACEFUL_STOP_TIMEOUT
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None \
                    and not any(_port_alive(p) for p in self.spec.ports):
                break
            time.sleep(0.3)
        still = self.proc is not None and self.proc.poll() is None
        if still or any(_port_alive(p) for p in self.spec.ports):
            if still:
                stop_tree(self.spec, self.proc.pid)   # 只杀自己 spawn 的进程树
            deadline = time.time() + 8
            while any(_port_alive(p) for p in self.spec.ports) and time.time() < deadline:
                time.sleep(0.3)
        if self.proc is not None:
            self.last_error = ""
            self.last_exit = self.proc.poll()
        self.proc = None
        self.managed = False
        self.ready = False
        self.state = "stopped"
        return {"ok": True, "state": "stopped", "message": "已停止"}

    # ---------------- 崩溃检测与恢复（工作线程内）----------------

    def _check_crash(self) -> None:
        if self.proc is None or self.state in ("stopping",):
            return
        code = self.proc.poll()
        if code is None:
            # 存活：core 健康后复位退避
            if self.ready is False and self.state == "running" and _health_ok(self.spec.health_url):
                self.ready = True
            if self.ready:
                self.backoff = BACKOFF_INITIAL
            return
        # 进程已退出
        self.ready = False
        self.managed = False
        was_tracked = self.proc
        self.proc = None
        if time.time() - self.started_at < CRASH_LOOP_WINDOW:
            self.state = "failed"
            self.last_error = f"启动后 {CRASH_LOOP_WINDOW}s 内退出 (exitcode={code})，暂停自动恢复"
            self.pending_recovery = None
            return
        if self.desired != "running":
            self.state = "stopped"
            return
        if not self.spec.auto_restart:
            self.state = "failed"
            self.last_error = f"意外退出 (exitcode={code})；该目标未启用自动恢复"
            return
        self.state = "failed"
        self.last_error = f"意外退出 (exitcode={code})，{self.backoff:.0f}s 后自动恢复"
        # 登记待执行恢复（携带当前 epoch；期间任何用户命令都会使其作废）
        self.pending_recovery = (self.epoch, time.time() + self.backoff)
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)
        _ = was_tracked

    def _check_recovery(self) -> None:
        if not self.pending_recovery:
            return
        epoch, run_at = self.pending_recovery
        if time.time() < run_at:
            return
        self.pending_recovery = None
        # 执行前重新核对意图与命令版本：期间 stop（epoch 变/desired=stopped）即作废
        if self.desired != "running" or epoch != self.epoch:
            self.state = "stopped" if self.desired == "stopped" else self.state
            print(f"[ctl:{self.spec.name}] 待执行恢复已作废（意图已变更）", flush=True)
            return
        if self._ports_busy_foreign():
            self.state = "conflict"
            return
        print(f"[ctl:{self.spec.name}] 自动恢复：重新启动", flush=True)
        result = self._cmd_start(epoch)
        print(f"[ctl:{self.spec.name}] {result.get('message', '')}", flush=True)


# ============================================================
# 管理服务（持有一组控制器 + 期望状态持久化）
# ============================================================

class SupervisorService:
    def __init__(self) -> None:
        self.epoch_lock = threading.Lock()
        self.desire_dir = log_root() / "desire"
        self.desire_dir.mkdir(parents=True, exist_ok=True)
        self.specs, self.config_errors = build_specs_safe()
        self.controllers = {n: TargetController(s, self) for n, s in self.specs.items()}

    # 期望状态持久化（跨进程/重启的意图记忆；所有权与串行化在控制器内）
    def read_desire(self, name: str) -> str:
        try:
            value = (self.desire_dir / f"{name}.txt").read_text(encoding="utf-8").strip()
            return value if value in ("running", "stopped", "restart") else "stopped"
        except OSError:
            return "stopped"

    def write_desire(self, name: str, value: str) -> None:
        self.desire_dir.mkdir(parents=True, exist_ok=True)
        (self.desire_dir / f"{name}.txt").write_text(value, encoding="utf-8")

    def snapshots(self) -> list[TargetSnapshot]:
        return [c.snapshot() for c in self.controllers.values()]

    def controller(self, name: str) -> TargetController | None:
        return self.controllers.get(name)

    def stop_everything(self) -> list[str]:
        msgs = []
        for c in sorted(self.controllers.values(), key=lambda c: c.spec.order, reverse=True):
            msgs.append(f"[{c.spec.name}] " + c.submit("stop").get("message", ""))
        return msgs
