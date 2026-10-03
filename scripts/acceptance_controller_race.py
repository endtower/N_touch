#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收（任务书 V05/V06/V07 + 第二轮复核 P1）：控制器并发与停止竞态。

用模拟进程与可注入探测，确定性验证：
  1. 并发 start ×10 —— 只 spawn 一个实例；
  2. 崩溃恢复退避期间 stop —— 待执行恢复作废，不复活；
  3. restart 与 stop 并发 —— 最后一个意图生效；
  4. 崩溃循环保护 —— 10s 内退出不自动恢复。

运行：python scripts/acceptance_controller_race.py
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import manager_core  # noqa: E402
from manager_core import SupervisorService  # noqa: E402
from core.supervisor import ProcSpec  # noqa: E402


class FakeProc:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self._died = False

    def poll(self):
        return self.returncode if self._died else None

    def die(self, code: int = 1):
        self._died = True
        self.returncode = code


def make_spec(tmp: Path, name: str = "core") -> ProcSpec:
    return ProcSpec(
        name=name,
        cmd=["fake"],
        cwd=tmp,
        out_log=tmp / f"{name}.out.log",
        ports=[5061],        # 测试专用端口，不与真实服务冲突
        health_url="",       # 用端口探测
        order=0,
        auto_restart=True,
        desc="测试目标",
    )


def install_mocks(spawn_log: list):
    """注入模拟 spawn / 端口探测 / 健康。端口开关由 port_open 控制。"""
    state = {"port_open": False, "next_pid": 100}

    def fake_spawn(spec, detached=False, clean_env=False):
        spawn_log.append(time.time())
        state["port_open"] = True
        state["next_pid"] += 1
        proc = FakeProc(state["next_pid"])
        state.setdefault("procs", []).append(proc)
        return proc

    def fake_port_alive(port, timeout=0.4):
        return state["port_open"]

    def fake_health(url):
        return False

    def fake_stop_tree(spec, pid):
        # 模拟强制终止：受管模拟进程死亡 + 端口关闭
        state["port_open"] = False
        for p in state.get("procs", []):
            p.die(0)
        return True, pid

    manager_core.stop_tree = fake_stop_tree
    manager_core.spawn = fake_spawn
    manager_core._port_alive = fake_port_alive
    manager_core._health_ok = fake_health
    return state


def wait_until(predicate, timeout=10, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="abyss_ctl_"))
    spawn_log: list = []
    state = install_mocks(spawn_log)
    mgr = SupervisorService()
    mgr.desire_dir = tmp / "desire"    # 隔离期望标记（不写真实数据目录）
    ctl = mgr.controller("core")
    ctl.spec = make_spec(tmp)          # 换成测试规格（端口/日志隔离）
    ctl._refresh_spec = lambda: None   # 冻结测试规格（禁用配置重读）
    ctl.backoff = 3.0                  # 缩短退避，测试用（窗口 3s 内可取消）
    failures = []

    # ---------- 1) 并发 start ×10 ----------
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(lambda _: ctl.submit("start"), range(10)))
    spawned = len(spawn_log)
    oks = sum(1 for r in results if r.get("ok"))
    if spawned != 1:
        failures.append(f"V05 并发 start×10 spawn 了 {spawned} 次（应为 1）")
    if oks != 10:
        failures.append(f"V05 并发 start×10 仅 {oks} 个成功")
    if not wait_until(lambda: ctl.state == "running", 5):
        failures.append("V05 start 后未进入 running")
    print(f"1) 并发 start×10: spawn={spawned} ok={oks}/10 state={ctl.state}")

    # ---------- 2) 崩溃 → 退避期 stop → 不复活 ----------
    ctl.started_at = time.time() - 20            # 绕开崩溃循环窗口
    state["port_open"] = False                   # 端口关闭 + 进程死亡 = 崩溃
    ctl.proc.die(1)
    if not wait_until(lambda: ctl.pending_recovery is not None, 5):
        failures.append("V06 崩溃后未登记待执行恢复")
    spawn_before = len(spawn_log)
    r = ctl.submit("stop")                       # 退避窗口内用户停止
    time.sleep(max(ctl.backoff + 1.5, 2.5))      # 等过原定恢复时间点
    if len(spawn_log) != spawn_before:
        failures.append("V06 退避期 stop 后仍复活（spawn 增加）")
    if ctl.desired != "stopped" or ctl.state != "stopped":
        failures.append(f"V06 stop 后期望/状态错误: {ctl.desired}/{ctl.state}")
    print(f"2) 崩溃→退避期 stop: 恢复作废, 最终={ctl.state}, desire={ctl.desired}")

    # ---------- 3) restart 与 stop 并发 ----------
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(ctl.submit, "restart"), ex.submit(ctl.submit, "stop")]
        list(ex.map(lambda f: f.result(timeout=60), futs))
    if ctl.state != "stopped" or ctl.desired != "stopped":
        failures.append(f"V07 restart/stop 并发后未停止: {ctl.state}/{ctl.desired}")
    print(f"3) restart+stop 并发: 最终={ctl.state}, desire={ctl.desired}")

    # ---------- 4) 崩溃循环保护 ----------
    spawn_before = len(spawn_log)
    r = ctl.submit("start")                      # spawn 后立即死（<10s）
    if not r.get("ok"):
        failures.append(f"V04 前置启动失败: {r}")
    ctl.proc.die(3)                              # 启动即死 → 崩溃循环保护
    time.sleep(6)                                # 覆盖扫描间隔与恢复到期
    if len(spawn_log) != spawn_before + 1:
        failures.append("V04 崩溃循环保护失效（spawn 次数异常）")
    if ctl.state != "failed":
        failures.append(f"V04 崩溃循环后状态应为 failed: {ctl.state}")
    print(f"4) 崩溃循环保护: spawn={len(spawn_log) - spawn_before} state={ctl.state}")

    if failures:
        print("\n验收失败:")
        for f in failures:
            print("  ✗", f)
        return 1
    print("\n验收通过：串行化控制器竞态修复成立")
    return 0


import tempfile  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
