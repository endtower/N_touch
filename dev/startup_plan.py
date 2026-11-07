"""Editable startup script for external AI harnesses; see docs/STARTUP.md.

Edit these ordinary Python functions for the actual installed programs. Secrets
stay in data/.env.dev. QQ launch/attach/login ordering belongs here, not in EXE.
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


async def start_snowluma(ctx):
    # Third-party example. An AI may insert supported QQ launch/injection/quick
    # login steps, or replace the chain for NapCat/NoneBot. Keep upstream storage.
    return await ctx.start_target("snowluma")


async def snow_connected(ctx):
    status = await ctx.ingress_status()
    return status.get("onebot_enabled") is True and status.get("onebot_connected") is True


async def wait_snowluma(ctx):
    if await snow_connected(ctx):
        return Outcome(message="QQ 接入已连接")
    return Outcome.waiting("请在 SnowLuma 页面完成 QQ 登录与连接，连接后自动继续。若接入未启用，请在平台接入中启用并应用。")


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
    return Outcome.waiting("请检查 Pi 桥接、登录与平台接入；修好后自动继续，也可点继续立即复查。接入服务若已停止重连，请重启消息接入。")


def build_plan(ctx):
    return (
        Chain("core", "主程序", (Step("services", "启动 Core 与消息接入", start_core),),
              required=True, description="已选平台共用的基础服务"),
        Chain("snowluma", "SnowLuma · QQ", (
            Step("runtime", "启动 SnowLuma", start_snowluma),
            Step("connection", "等待 QQ 登录与连接", wait_snowluma, probe=snow_connected),
        ), dependencies=("core",), enabled=ctx.enabled("ONEBOT_ENABLED", True),
              description="第三方程序示例，使用它自己的登录与配置页面"),
        Chain("pi", "Pi · 个人桥接", (
            Step("tunnel", "建立 Pi 隧道并检查桥接服务", start_pi, timeout=90),
            Step("connection", "等待 Pi 平台链路就绪", wait_pi, probe=pi_connected),
        ), dependencies=("core",), enabled=ctx.enabled("BRIDGE_ENABLED"),
              description="自研程序示例；失败可跳过，不影响 QQ 链路"),
    )
