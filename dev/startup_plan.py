"""Editable startup script for external AI harnesses; see docs/STARTUP.md.

Edit these ordinary Python functions for the actual installed programs. Secrets
stay in data/.env.dev. QQ launch/attach/login ordering belongs here, not in EXE.

设计原则（改这个文件时请守住）：
1. 用户点「一键启动」之后，能用只读探测自动判断的一律用 probe 自动放行，
   不要让他再点一次「继续」。
2. 需要用户动手的只留真正只有他能做的事（登录账号、在第三方页面点确认），
   提示语写清「要做什么」，而不是「去检查配置」。
3. **绝不抢用户的操作**：不自动打开浏览器、不模拟点击/输入、不把窗口拉到前台、
   不自动切 QQ 账号。只做「启动进程 + 只读探测 + 等他自己做完」。
4. 已经在跑的不重启；探测不到不等于失败，停在等待态让用户随时点继续。
"""
import asyncio
import os

import aiohttp
from dotenv import dotenv_values
from paths import data_root
from startup_workflow import Chain, Outcome, Step


class StartupContext:
    def __init__(self, service):
        self.service = service

    def values(self):
        return {**os.environ, **dict(dotenv_values(data_root() / ".env.dev"))}

    def enabled(self, key, default=False):
        return str(self.values().get(key, str(default))).lower() in ("true", "1", "yes", "on")

    def clean(self, text):
        for key, value in self.values().items():
            if value and len(str(value)) >= 4 and any(word in key.upper() for word in ("TOKEN", "PASSWORD", "API_KEY", "SECRET")):
                text = text.replace(str(value), "[已隐藏]")
        return text

    async def start_target(self, name):
        controller = self.service.ensure_target(name)
        if controller is None:
            raise ValueError("尚未配置目标：" + name + "；请配置后点击继续")
        result = await asyncio.to_thread(controller.submit, "start")
        if not result.get("ok"):
            raise RuntimeError(self.clean(result.get("message", "启动失败")))
        if not controller.snapshot().ready:
            raise RuntimeError("服务尚未就绪，请检查后点击继续")
        return Outcome(message="服务已就绪")

    async def ingress_status(self):
        controller = self.service.controller("ingress")
        if controller is None:
            return {}
        url = controller.spec.health_url.rsplit("/", 1)[0] + "/v1/status"
        try:
            token = (data_root() / "manager/service_token.txt").read_text(encoding="utf-8").strip()
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=4)) as client:
                async with client.get(url, headers={"X-Manager-Token": token}, allow_redirects=False) as response:
                    if response.status == 200:
                        body = await response.json()
                        if body.get("service") == "abyss-ingress" and body.get("instance") == str(data_root()):
                            return body
        except (OSError, aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            pass
        return {}


async def start_core(ctx):
    return await ctx.start_target("core")


def snowluma_installed():
    """SnowLuma 是否真的装好了。没装时整条 QQ 链路不参与，不要报错吓新用户。

    只读判断，不启动任何东西，也不去碰它的目录。
    """
    try:
        from core.supervisor import snowluma_root
        root = snowluma_root()
    except Exception:
        return False
    return (root / "node.exe").is_file() and (root / "index.mjs").is_file()


async def start_snowluma(ctx):
    # 只负责把 SnowLuma 进程拉起来。登录、点「确认」这类只有用户能做的事
    # 交给下面的等待步骤；这里不开浏览器、不碰他的鼠标。
    return await ctx.start_target("snowluma")


async def snow_connected(ctx):
    status = await ctx.ingress_status()
    return status.get("onebot_enabled") is True and status.get("onebot_connected") is True


async def wait_snowluma(ctx):
    if await snow_connected(ctx):
        return Outcome(message="QQ 接入已连接")
    status = await ctx.ingress_status()
    if not status.get("onebot_enabled"):
        # 这一步不用用户去翻设置：只要他自己没关，就提示一句开关位置。
        return Outcome.waiting("QQ 接入未启用。请在「系统 → 平台接入」打开 SnowLuma 开关并保存，Core 会自动重连。")
    return Outcome.waiting("请在 SnowLuma 页面登录助手号并确认连接；连上后自动继续，不用再点任何按钮。")


async def start_pi(ctx):
    # First-party example. Reuse configured SSH and end-to-end bridge health.
    # An AI can insert this personal program's remote daemon launch before it.
    return await ctx.start_target("tunnel")


async def pi_connected(ctx):
    bridge = (await ctx.ingress_status()).get("bridge", {})
    return bridge.get("enabled") is True and bridge.get("ready") is True and not bridge.get("blocked")


async def wait_pi(ctx):
    if await pi_connected(ctx):
        return Outcome(message="Pi 桥接与主程序已连接")
    return Outcome.waiting("Pi 桥接还没就绪。修好后自动继续，也可点「继续」立即复查。")


def build_plan(ctx):
    return (
        Chain("core", "主程序", (Step("services", "启动 Core 与消息接入", start_core),),
              required=True, description="已选平台共用的基础服务"),
        # 两条链路都只留最少步骤：起进程 + 等用户做完只有他能做的事。
        # probe 会自己轮询，用户登录完就自动往下走，不需要再点「继续」。
        # 没装 SnowLuma 时整条链路默认不参与：新用户点一键启动只起 Core，
        # 不会被一条还没装的 QQ 链路挡住。
        Chain("snowluma", "SnowLuma · QQ", (
            Step("runtime", "启动 SnowLuma", start_snowluma),
            Step("connection", "等待 QQ 登录与连接", wait_snowluma, probe=snow_connected, poll_interval=3),
        ), dependencies=("core",),
              enabled=ctx.enabled("ONEBOT_ENABLED", True) and snowluma_installed(),
              description="自动启动 SnowLuma；你只需在它的页面登录助手号"),
        Chain("pi", "Pi · 个人桥接", (
            Step("tunnel", "建立 Pi 隧道并检查桥接服务", start_pi, timeout=90),
            Step("connection", "等待 Pi 平台链路就绪", wait_pi, probe=pi_connected),
        ), dependencies=("core",), enabled=ctx.enabled("BRIDGE_ENABLED"),
              description="自研程序示例；失败可跳过，不影响 QQ 链路"),
    )
