"""Independent, resumable startup chains. Edit startup_plan.py to customize steps.

All process ownership remains with SupervisorService. Opening the launcher or
reading status never executes this plan. Only authenticated commands do so.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import time
from typing import Callable

from aiohttp import web


@dataclass(frozen=True)
class Outcome:
    state: str = "done"
    message: str = "已完成"

    @classmethod
    def waiting(cls, message):
        return cls("waiting", message)


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    run: Callable
    probe: Callable | None = None
    timeout: float = 150
    poll_interval: float = 2


@dataclass(frozen=True)
class Chain:
    id: str
    title: str
    steps: tuple[Step, ...]
    dependencies: tuple[str, ...] = ()
    enabled: bool = True
    required: bool = False
    description: str = ""


@dataclass
class Progress:
    definition: Chain
    enabled: bool
    state: str = "idle"
    index: int = 0
    message: str = "尚未启动"
    steps: list[dict] = field(default_factory=list)
    selected: bool = False


class Workflow:
    def __init__(self, definitions, context, settings: Path, log: Path):
        self.context, self.settings, self.log = context, settings, log
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.events = []
        self.closed = False
        self.paused = False
        self.chains = {}
        try:
            saved = json.loads(settings.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        for definition in definitions:
            if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", definition.id) or definition.id in self.chains:
                raise ValueError("启动链路标识无效或重复")
            if not definition.steps or len({s.id for s in definition.steps}) != len(definition.steps):
                raise ValueError("链路需要步骤，步骤标识不能重复")
            enabled = True if definition.required else bool(saved.get(definition.id, definition.enabled))
            self.chains[definition.id] = Progress(definition, enabled, steps=[
                {"id": s.id, "title": s.title, "state": "pending", "message": ""} for s in definition.steps])
        def visit(name, ancestors):
            if name not in self.chains or name in ancestors:
                raise ValueError("启动链路存在未知依赖或循环依赖")
            for dependency in self.chains[name].definition.dependencies:
                visit(dependency, ancestors | {name})
        for name in self.chains:
            visit(name, set())

    def emit(self, progress, message):
        message = self.context.clean(str(message))[:1000]
        progress.message = message
        event = {"time": time.time(), "chain": progress.definition.id,
                 "state": progress.state, "message": message}
        self.events.append(event)
        self.events[:] = self.events[-100:]
        self.log.parent.mkdir(parents=True, exist_ok=True)
        # Store a bounded snapshot; no environment values, arguments or credentials.
        temp = self.log.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(self.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.log)
        except OSError:
            pass  # A log file in use must not break an otherwise working chain.

    def snapshot(self):
        chains = [{"id": p.definition.id, "title": p.definition.title,
                   "description": p.definition.description, "dependencies": list(p.definition.dependencies),
                   "enabled": p.enabled, "required": p.definition.required, "state": p.state,
                   "message": p.message, "step_index": p.index, "steps": [dict(s) for s in p.steps],
                   "can_continue": p.state in ("waiting", "failed", "blocked", "skipped", "cancelled"),
                   "can_skip": not p.definition.required and p.state in ("pending", "running", "waiting", "failed", "blocked")}
                  for p in self.chains.values()]
        selected = [p for p in self.chains.values() if p.selected]
        states = {p.state for p in selected}
        state = "idle"
        if "running" in states or "pending" in states:
            state = "running"
        elif states & {"failed", "blocked", "waiting", "cancelled"}:
            state = "attention"
        elif "skipped" in states:
            state = "partial"
        elif selected and states == {"done"}:
            state = "done"
        if self.paused and selected:
            state = "paused"
        return {"state": state, "paused": self.paused, "chains": chains, "events": list(self.events)}

    def _select(self, name):
        p = self.chains[name]
        for dependency in p.definition.dependencies:
            self._select(dependency)
        p.selected = True
        if p.state not in ("done", "running", "waiting"):
            p.state = "pending"

    def _schedule(self):
        if self.closed or self.paused:
            return
        for name, p in self.chains.items():
            if not p.selected or p.state not in ("pending", "blocked"):
                continue
            blocked = [self.chains[d] for d in p.definition.dependencies if self.chains[d].state != "done"]
            if blocked:
                p.state = "blocked"
                p.message = "等待前置链路：" + "、".join(d.definition.title for d in blocked)
                continue
            p.state = "running"
            self.tasks[name] = asyncio.create_task(self._run(name))

    async def _run(self, name):
        p = self.chains[name]
        try:
            while p.index < len(p.definition.steps):
                step = p.definition.steps[p.index]
                record = p.steps[p.index]
                record["state"] = "running"
                self.emit(p, step.title)
                outcome = await asyncio.wait_for(step.run(self.context), step.timeout)
                if not isinstance(outcome, Outcome) or outcome.state not in ("done", "waiting", "failed"):
                    raise ValueError("启动步骤必须返回 Outcome")
                record.update(state=outcome.state, message=self.context.clean(outcome.message))
                p.state = "running" if outcome.state == "done" else outcome.state
                self.emit(p, outcome.message)
                if outcome.state == "failed":
                    return
                if outcome.state == "waiting":
                    if step.probe is None:
                        return
                    while True:
                        await asyncio.sleep(max(0.05, step.poll_interval))
                        checked = await asyncio.wait_for(step.probe(self.context), min(step.timeout, 15))
                        if checked:
                            record.update(state="done", message="检查通过")
                            p.state = "running"
                            break
                p.index += 1
            p.state = "done"
            self.emit(p, "链路已就绪")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            p.state = "failed"
            message = "步骤等待超时，请检查后重试" if isinstance(error, TimeoutError) else f"{type(error).__name__}: {error}"
            if p.index < len(p.steps):
                p.steps[p.index].update(state="failed", message=self.context.clean(message))
            self.emit(p, message)
        finally:
            # Only this generation owns this slot; stale tasks cannot start successors.
            if self.tasks.get(name) is asyncio.current_task():
                self.tasks.pop(name, None)
                self._schedule()

    async def command(self, action, name=None, enabled=None):
        async with self.lock:
            if self.closed:
                raise ValueError("启动服务正在退出")
            if name is not None and name not in self.chains:
                raise ValueError("未知启动链路")
            if action == "enable":
                if name is None or not isinstance(enabled, bool):
                    raise ValueError("请选择链路及启用状态")
                p = self.chains[name]
                if p.definition.required and not enabled:
                    raise ValueError("公共基础链路必须启用")
                p.enabled = enabled
                self.settings.parent.mkdir(parents=True, exist_ok=True)
                temp = self.settings.with_suffix(".tmp")
                temp.write_text(json.dumps({n: v.enabled for n, v in self.chains.items()}), encoding="utf-8")
                temp.replace(self.settings)
                return self.snapshot()
            if action == "cancel":
                self.paused = True
                cancelled = list(self.tasks.items())
                self.tasks.clear()
                for n, task in cancelled:
                    task.cancel()
                    p = self.chains[n]
                    p.state = "cancelled"
                    self.emit(p, "启动已暂停；已运行程序保持当前状态")
                await asyncio.gather(*(task for _, task in cancelled), return_exceptions=True)
                for p in self.chains.values():
                    if p.state in ("pending", "blocked"):
                        p.state = "cancelled"
                return self.snapshot()
            if action == "skip":
                if name is None or self.chains[name].definition.required:
                    raise ValueError("公共基础链路不能跳过")
                p = self.chains[name]
                if p.state == "done":
                    raise ValueError("链路已完成，无需跳过")
                task = self.tasks.pop(name, None)
                p.state = "skipped"
                if task:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                self.emit(p, "本轮已跳过，可稍后单独继续")
                self._schedule()
                return self.snapshot()
            if action not in ("start", "continue", "retry"):
                raise ValueError("未知启动操作")
            self.paused = False
            names = [name] if name else [n for n, p in self.chains.items() if p.enabled]
            new_run = not any(p.selected and p.state not in ("done", "skipped") for p in self.chains.values())
            for n in names:
                p = self.chains[n]
                if n in self.tasks:
                    # Continue performs a fresh check now, without replaying completed steps.
                    if action != "continue" or p.state != "waiting":
                        continue
                    task = self.tasks.pop(n)
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if p.state == "done":
                    if name is None and not new_run:
                        continue
                    # Recheck completed chains on an explicit new start: steps are idempotent.
                    p.index = 0
                    for record in p.steps:
                        record.update(state="pending", message="")
                p.state = "pending"
                self._select(n)
            self._schedule()
            return self.snapshot()

    async def close(self):
        await self.command("cancel")
        self.closed = True


def plan_source():
    """用户的启动脚本优先，官方随包的最简版兜底。

    用户（或他请的 AI）改写启动流程的位置固定在 data/manager/startup_plan.py：
    它属于用户数据，永不随组件更新被覆盖，也不进版本库。data 里没有这份文件时
    才使用官方安装目录里的最简版，保证全新安装开箱即用。
    """
    from paths import code_root, data_root
    custom = data_root() / "manager" / "startup_plan.py"
    if custom.is_file():
        return custom
    return code_root() / "startup_plan.py"


def load_workflow(service, tolerate_errors=False):
    """Load the editable startup script, including edits made within a second."""
    from paths import data_root, log_root
    source = plan_source()
    import sys
    from types import ModuleType
    module = ModuleType("abyss_startup_plan")
    module.__file__ = str(source)
    previous = sys.modules.get(module.__name__)
    sys.modules[module.__name__] = module
    namespace = module.__dict__
    try:
        exec(compile(source.read_text(encoding="utf-8-sig"), str(source), "exec"), namespace)
        context = namespace["StartupContext"](service)
        return Workflow(namespace["build_plan"](context), context,
                        data_root() / "manager/startup-settings.json",
                        log_root() / "manager/startup-state.json")
    except Exception as error:
        if previous is None:
            sys.modules.pop(module.__name__, None)
        else:
            sys.modules[module.__name__] = previous
        if not tolerate_errors:
            raise
        # An AI's half-written plan must not take down configuration and logs.
        message = "启动脚本需要修复：" + type(error).__name__
        async def unavailable(_context):
            return Outcome("failed", message)
        class Context:
            @staticmethod
            def clean(text):
                return str(text)
        return Workflow((Chain("configuration", "启动脚本待修复", (
            Step("repair", "请让配置此程序的 AI 修复启动脚本并重新加载", unavailable),
        ), required=True, description=message),), Context(),
            data_root() / "manager/startup-settings.json", log_root() / "manager/startup-state.json")


def register_startup(app, workflow, authorized, service):
    async def status(request):
        if not authorized(request):
            return web.json_response({"message": "管理令牌缺失或不匹配"}, status=401)
        return web.json_response(workflow.snapshot())

    async def command(request):
        nonlocal workflow
        if not authorized(request):
            return web.json_response({"message": "管理令牌缺失或不匹配"}, status=401)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("请求体必须是对象")
            action = request.match_info["action"]
            if action == "reload":
                if workflow.tasks:
                    raise ValueError("请先暂停启动流程再重新加载脚本，已运行的程序不受影响")
                try:
                    replacement = load_workflow(service)
                except Exception as error:
                    raise ValueError("启动脚本加载失败：" + type(error).__name__) from None
                workflow = replacement
                service.startup_workflow = workflow
                return web.json_response(workflow.snapshot())
            if action == "stop":
                guard = getattr(service, "update_guard", None)
                if guard and any(guard(n) for n in service.controllers):
                    raise ValueError("组件正在更新，请完成更新后再停止全部")
                await workflow.command("cancel")
                await service.stop_everything_async()
                for p in workflow.chains.values():
                    p.selected = False
                    p.state = "idle"
                    p.index = 0
                    for step in p.steps:
                        step.update(state="pending", message="")
                    workflow.emit(p, "已停止，可以重新启动")
                return web.json_response(workflow.snapshot())
            return web.json_response(await workflow.command(action, body.get("chain"), body.get("enabled")))
        except (ValueError, KeyError, TypeError) as error:
            return web.json_response({"ok": False, "message": str(error)}, status=400)

    async def cleanup(_app):
        await workflow.close()
    app.router.add_get("/v1/startup", status)
    app.router.add_post("/v1/startup/{action:start|continue|retry|skip|cancel|enable|stop|reload}", command)
    app.on_cleanup.append(cleanup)
