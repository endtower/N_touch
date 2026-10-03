#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manager_core —— 目标进程串行化控制器（独立管理服务的核心）

第二轮复核 R1-R3 修复要点：
1. 优雅关闭单向：控制器对 core 的停止调用 Core 的内部直达入口
   /system/core/shutdown-internal（仅让 uvicorn 自己退出，绝不回调管理服务）；
   网页面板的 /system/core/* 已全部代理到本服务，不再产生循环。
2. 停止以"退出事实"确认：taskkill/超时后仍未退出 → 保留句柄与托管归属，
   返回失败与真实状态；进程已退但端口仍开放 → 区分"目标已停/外部占用"。
3. 命令版本与入队原子化：epoch 分配和 queue.put 在同一临界区内完成，
   队列顺序 == epoch 顺序；执行时 epoch 不等于当前值即按过时命令跳过。
   工作循环每轮先处理用户命令，再做崩溃检测与恢复，消除登记竞态。
4. 健康等待可取消：等待循环每轮检查命令是否过时，过时即终止刚拉起的
   进程并返回取消，不会把 stop 请求困在 30s 等待里。
5. 环境净化：spawn 时从继承环境剔除配置覆盖键，子进程从 .env.dev 重新
   读取最新值（避免服务进程的旧环境覆盖用户刚保存的配置）。
"""

from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from paths import app_root, data_root, log_root
from core.supervisor import (
    ProcSpec,
    build_specs,
    core_cmd,
    spawn,
    stop_tree,
)

STATES = ("stopped", "starting", "running", "stopping", "failed", "conflict")

HEALTH_WAIT_SEC = 30
GRACEFUL_STOP_TIMEOUT = 10
CRASH_LOOP_WINDOW = 10
BACKOFF_INITIAL = 5
BACKOFF_MAX = 60


# ============================================================
# 配置容错的规格构建
# ============================================================

def build_specs_safe() -> tuple[dict[str, ProcSpec], list[str]]:
    """Core 配置非法时降级为 core.exe/默认端口并记录错误，管理服务不崩。"""
    errors: list[str] = []
    try:
        return build_specs(), errors
    except Exception as e:
        errors.append(f"Core 配置加载失败（{type(e).__name__}: {e}），已降级运行")
    web_port, onebot_port = 5002, 5001
    try:
        from core.supervisor import core_ports
        web_port, onebot_port = core_ports()
    except Exception:
        pass
    from core.supervisor import SNOWLUMA_DIR, LOG_DIR
    specs = {
        "core": ProcSpec(
            name="core", cmd=core_cmd(), cwd=app_root(),
            out_log=LOG_DIR / "core.out.log",
            ports=[web_port, onebot_port],
            health_url=f"http://127.0.0.1:{web_port}/health",
            order=0, desc="Abyss Core（降级端口）",
        ),
        "snowluma": ProcSpec(
            name="snowluma", cmd=["cmd", "/c", "launcher.bat"], cwd=SNOWLUMA_DIR,
            out_log=LOG_DIR / "snowluma.out.log",
            ports=[5099, 3001], order=1, auto_restart=False,
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


def _clean_env() -> dict:
    """继承环境但剔除配置覆盖键：子进程从 .env.dev 读取最新值（复核 R4-4）。"""
    from config.env_tools import CONFIG_OVERRIDE_KEYS
    env = os.environ.copy()
    for key in CONFIG_OVERRIDE_KEYS:
        env.pop(key, None)
    return env


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
    """单个目标的唯一控制者：专属工作线程串行处理全部命令与崩溃恢复。"""

    def __init__(self, spec: ProcSpec, service: "SupervisorService"):
        self.spec = spec
        self.service = service
        self.queue: queue.Queue = queue.Queue()
        self.state = "stopped"
        self.desired = service.read_desire(spec.name)
        self.ready = False
        self.managed = False
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.restarts = 0
        self.last_error = ""
        self.epoch = 0
        self.pending_recovery: tuple[int, float] | None = None
        self.backoff = BACKOFF_INITIAL
        self._stop_event = threading.Event()
        threading.Thread(target=self._run, daemon=True, name=f"ctl-{spec.name}").start()

    # ---------- 对外命令：epoch 分配与入队原子化（复核 R3）----------

    def submit(self, kind: str) -> dict:
        if kind not in ("start", "stop", "restart"):
            return {"ok": False, "error_code": "bad_command", "message": f"未知命令 {kind}"}
        done = threading.Event()
        result: dict = {}
        with self.service.epoch_lock:
            self.epoch += 1
            epoch = self.epoch
            desired = {"start": "running", "stop": "stopped", "restart": "running"}[kind]
            self.desired = desired
            self.service.write_desire(self.spec.name, desired)
            self.queue.put((kind, epoch, desired, done, result))
        done.wait(timeout=HEALTH_WAIT_SEC + GRACEFUL_STOP_TIMEOUT + 20)
        if not result:
            return {"ok": False, "error_code": "timeout",
                    "message": "命令排队超时（目标正被其他操作占用），稍后重试"}
        return result

    def snapshot(self) -> TargetSnapshot:
        alive = self.proc is not None and self.proc.poll() is None
        uptime = time.time() - self.started_at if (alive and self.started_at) else None
        return TargetSnapshot(
            name=self.spec.name, state=self.state, desired=self.desired,
            pid=self.proc.pid if alive else None, ports=list(self.spec.ports),
            ready=self.ready, managed=self.managed, restarts=self.restarts,
            last_error=self.last_error, uptime_sec=round(uptime, 1) if uptime else None,
        )

    # ---------- 工作线程：用户命令优先，再做崩溃/恢复 ----------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            item = None
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                pass
            if item is not None:
                kind, epoch, desired_snapshot, done, result = item
                try:
                    # 过时判定（复核 R3）：意图快照与当前期望不一致才算过时
                    # （较新的 stop 已生效时，迟到的 start 不应再执行）；
                    # 相同意图的重复命令（连点 start）返回"已在运行"即为正确结果
                    if desired_snapshot != self.desired:
                        result.update({"ok": False, "error_code": "superseded",
                                       "state": self.state,
                                       "message": "命令已过时（有更新的操作生效）"})
                    elif kind == "start":
                        result.update(self._cmd_start(epoch))
                    elif kind == "stop":
                        result.update(self._cmd_stop(epoch))
                    elif kind == "restart":
                        r1 = self._cmd_stop(epoch)
                        result.update(r1)
                        if r1.get("ok") or r1.get("error_code") == "not_running":
                            result.update(self._cmd_start(epoch))
                except Exception as e:
                    result.update({"ok": False, "error_code": "internal",
                                   "message": f"{type(e).__name__}: {e}"})
                finally:
                    done.set()
                continue
            # 无用户命令时才做崩溃检测与到期恢复
            self._check_crash()
            self._check_recovery()

    # ---------- 命令实现 ----------

    def _refresh_spec(self) -> None:
        """每次 start 前重读配置（端口/路径变更 → 下一次启动即生效，复核 R4-4）。"""
        try:
            specs, errors = build_specs_safe()
            if self.spec.name in specs:
                self.spec = specs[self.spec.name]
        except Exception:
            pass

    def _cmd_start(self, epoch: int) -> dict:
        self._refresh_spec()
        if self.proc is not None and self.proc.poll() is None:
            self.state = "running" if self.ready else "starting"
            return {"ok": True, "state": self.state, "pid": self.proc.pid,
                    "message": "目标进程已在（本服务管理），不重复创建"}
        if any(_port_alive(p) for p in self.spec.ports):
            self.state = "conflict"
            self.last_error = "端口被外部程序占用（不是本服务启动的实例），不自动接管"
            return {"ok": False, "error_code": "conflict", "state": self.state,
                    "message": self.last_error}
        self.state = "starting"
        self.last_error = ""
        try:
            proc = spawn(self.spec, detached=False, clean_env=True)
        except Exception as e:
            self.state = "failed"
            self.last_error = f"spawn 失败: {e}"
            return {"ok": False, "error_code": "spawn_failed", "state": self.state,
                    "message": self.last_error}
        self.proc = proc
        self.started_at = time.time()
        self.managed = True
        # 健康等待可取消：每轮核对命令版本（复核 R3——不把 stop 困在等待里）
        deadline = time.time() + HEALTH_WAIT_SEC
        while time.time() < deadline:
            if proc.poll() is not None:
                self.state = "failed"
                self.ready = False
                self.last_error = f"启动后立即退出 (exitcode={proc.returncode})"
                return {"ok": False, "error_code": "exited", "state": self.state,
                        "message": self.last_error}
            if epoch != self.epoch or self.desired != "running":
                # 有更新意图：终止刚拉起的进程（它属于本控制器），返回取消
                stop_tree(self.spec, proc.pid)
                self.proc = None
                self.managed = False
                self.state = "stopped" if self.desired == "stopped" else self.state
                return {"ok": False, "error_code": "superseded", "state": self.state,
                        "message": "启动已取消（有更新的操作生效）"}
            if _health_ok(self.spec.health_url) if self.spec.health_url \
                    else any(_port_alive(p) for p in self.spec.ports):
                self.state = "running"
                self.ready = True
                self.backoff = BACKOFF_INITIAL
                return {"ok": True, "state": "running", "pid": proc.pid,
                        "message": "已启动并就绪"}
            time.sleep(0.4)
        self.state = "failed"
        self.ready = False
        self.last_error = f"{HEALTH_WAIT_SEC}s 内未就绪（进程保持追踪，可停止后重试）"
        return {"ok": False, "error_code": "not_ready", "state": self.state,
                "pid": proc.pid, "message": self.last_error}

    def _cmd_stop(self, epoch: int) -> dict:
        pid = self.proc.pid if (self.proc is not None and self.proc.poll() is None) else None
        self.state = "stopping"
        if pid is None:
            if any(_port_alive(p) for p in self.spec.ports):
                # 进程已死但端口仍开放：目标已停 + 外部占用，两件事分开说（复核 R2）
                self.state = "stopped"
                self.ready = False
                self.managed = False
                return {"ok": True, "error_code": "external_port",
                        "state": "stopped",
                        "message": "目标已停止，但端口仍被外部程序占用（未处理外部进程）"}
            self.state = "stopped"
            self.ready = False
            self.managed = False
            return {"ok": True, "error_code": "not_running", "state": "stopped",
                    "message": "目标未在运行"}
        # 优雅优先：core 走内部直达关闭入口（只让 uvicorn 自己退出，无回调循环）
        if self.spec.name == "core" and _health_ok(self.spec.health_url):
            try:
                base = self.spec.health_url.rsplit("/", 1)[0]
                req = urllib.request.Request(base + "/system/core/shutdown-internal",
                                             method="POST")
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass
        # 以退出事实确认（复核 R2）
        deadline = time.time() + GRACEFUL_STOP_TIMEOUT
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None \
                    and not any(_port_alive(p) for p in self.spec.ports):
                break
            time.sleep(0.3)
        alive = self.proc is not None and self.proc.poll() is None
        if alive or any(_port_alive(p) for p in self.spec.ports):
            if alive:
                stop_tree(self.spec, self.proc.pid)   # 仅终止自己持有的进程
            deadline = time.time() + 8
            while any(_port_alive(p) for p in self.spec.ports) and time.time() < deadline:
                time.sleep(0.3)
        still_alive = self.proc is not None and self.proc.poll() is None
        if still_alive:
            # 停止失败：保留句柄与托管归属，返回失败与真实状态（复核 R2）
            self.state = "failed"
            self.last_error = "停止失败：进程在优雅等待与强制终止后仍然存活"
            return {"ok": False, "error_code": "stop_failed", "state": self.state,
                    "pid": self.proc.pid, "message": self.last_error}
        exited = self.proc.poll() if self.proc is not None else None
        self.proc = None
        self.managed = False
        self.ready = False
        self.state = "stopped"
        note = "" if not any(_port_alive(p) for p in self.spec.ports) \
            else "目标已退出，但端口仍被外部程序占用"
        return {"ok": True, "state": "stopped", "exited": exited,
                "message": ("已停止" + (f"（{note}）" if note else ""))}

    # ---------- 崩溃检测与恢复 ----------

    def _check_crash(self) -> None:
        if self.proc is None or self.state == "stopping":
            return
        code = self.proc.poll()
        if code is None:
            if self.state == "running" and not self.ready \
                    and _health_ok(self.spec.health_url):
                self.ready = True
            if self.ready:
                self.backoff = BACKOFF_INITIAL
            return
        self.ready = False
        self.managed = False
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
        self.pending_recovery = (self.epoch, time.time() + self.backoff)
        self.backoff = min(self.backoff * 2, BACKOFF_MAX)

    def _check_recovery(self) -> None:
        if not self.pending_recovery:
            return
        epoch, run_at = self.pending_recovery
        if time.time() < run_at:
            return
        self.pending_recovery = None
        # 执行前重核：期间任何用户命令（epoch 变化）或意图变化都使恢复作废
        if self.desired != "running" or epoch != self.epoch:
            self.state = "stopped" if self.desired == "stopped" else self.state
            print(f"[ctl:{self.spec.name}] 待执行恢复已作废（意图已变更）", flush=True)
            return
        if any(_port_alive(p) for p in self.spec.ports):
            self.state = "conflict"
            return
        print(f"[ctl:{self.spec.name}] 自动恢复：重新启动", flush=True)
        result = self._cmd_start(epoch, "running")
        print(f"[ctl:{self.spec.name}] {result.get('message', '')}", flush=True)


# ============================================================
# 管理服务
# ============================================================

class SupervisorService:
    def __init__(self) -> None:
        self.epoch_lock = threading.Lock()
        self.desire_dir = log_root() / "desire"
        self.desire_dir.mkdir(parents=True, exist_ok=True)
        self.specs, self.config_errors = build_specs_safe()
        self.controllers = {n: TargetController(s, self) for n, s in self.specs.items()}

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

    async def stop_everything_async(self) -> list[str]:
        return await asyncio.to_thread(self.stop_everything)
