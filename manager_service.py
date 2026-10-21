#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""manager_service —— Abyss 独立常驻管理服务（无界面入口，打包为 manager.exe）

唯一进程控制者：WPF 窗口、CLI、Core 网页系统面板都通过本服务的 /v1 接口
提交命令。不导入 SystemCore/业务代码，不初始化业务数据库/人格/LLM；
Core 配置非法时服务照常运行（降级端口 + 错误上报）。

接口（仅监听 127.0.0.1，需 X-Manager-Token 头）：
    GET  /v1/info                    服务身份/版本/数据目录
    GET  /v1/targets                 全部目标状态快照
    POST /v1/targets/{t}/start|stop|restart    串行化启停命令
    GET  /v1/logs/{source}?lines=    日志尾部（core/core_out/snowluma/service）
    POST /v1/shutdown                停止全部受管目标并退出服务
配置读写（R5 更新前的最小配置面）：
    GET/PUT /v1/config/core          .env.dev 白名单字段（端口/路径/token）

令牌：首次启动生成 data_root/manager/service_token.txt；WPF 与 CLI 从同一路径读取。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import secrets
import sys
import time
from pathlib import Path

from paths import app_root, data_root, ensure_cwd, log_root

app_root()
ensure_cwd()
sys.path.insert(0, str(app_root()))

from aiohttp import web                                     # noqa: E402
import aiohttp                                               # noqa: E402
from manager_core import SupervisorService, log_root        # noqa: E402
from config.llm_settings import (DEFAULT_COMMON_URL, DEFAULT_COMMON_MODEL,
                                 DEFAULT_EMBEDDING_MODEL, resolve_provider_label)  # noqa: E402

SERVICE_VERSION = "0.9.0"
API_PORT = 49990
_TOKEN_FILE = data_root() / "manager" / "service_token.txt"

_service: SupervisorService | None = None
_started_at = time.time()


# ============================================================
# 令牌与鉴权
# ============================================================

def _load_or_create_token() -> str:
    try:
        token = _TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            return token
    except OSError:
        pass
    token = secrets.token_urlsafe(24)
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token, encoding="utf-8")
    return token


TOKEN = _load_or_create_token()


def _authorized(request: web.Request) -> bool:
    return request.headers.get("X-Manager-Token", "") == TOKEN


def _denied() -> web.Response:
    return web.json_response({"error_code": "unauthorized", "message": "管理令牌缺失或不匹配"}, status=401)


def _target(request: web.Request):
    name = request.match_info.get("target", "")
    ctrl = _service.controller(name)
    return ctrl, (None if ctrl else f"未知目标 {name}（可选 ingress | core | snowluma）")


# ============================================================
# 路由
# ============================================================

async def handle_info(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    return web.json_response({
        "service": "abyss-manager",
        "version": SERVICE_VERSION,
        "api": "v1",
        "instance": str(data_root()),
        "uptime_sec": round(time.time() - _started_at, 1),
        "config_errors": _service.config_errors,
    })


async def handle_targets(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    targets = [vars(s) for s in _service.snapshots()]
    for target in targets:
        if target["name"] == "ingress" and target["ready"]:
            from aiohttp import ClientSession, ClientTimeout
            try:
                base = _service.controller("ingress").spec.health_url.rsplit("/", 1)[0]
                async with ClientSession(timeout=ClientTimeout(total=1)) as client:
                    async with client.get(base + "/v1/status", headers={"X-Manager-Token": TOKEN}) as response:
                        if response.status == 200:
                            detail = await response.json()
                            target["ready"] = bool(detail.get("ready"))
                            target["onebot_connected"] = detail.get("onebot_connected")
                            target["pending"] = detail.get("pending", {})
                            if detail.get("last_error"):
                                target["last_error"] = detail["last_error"]
            except Exception:
                target["onebot_connected"] = None
                target["ready"] = False
    return web.json_response({
        "targets": targets,
        "config_errors": _service.config_errors,
        "service_started_at": _started_at,
    })


async def handle_target_cmd(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    ctrl, err = _target(request)
    if ctrl is None:
        return web.json_response({"error_code": "bad_target", "message": err}, status=404)
    kind = request.match_info["action"]
    # 控制器内部是线程同步等待（健康等待最长 30s+），转线程执行，
    # 保证服务事件循环在启停期间仍可响应状态/日志/停止请求（复核 R1）
    result = await asyncio.to_thread(ctrl.submit, kind)
    status = 200 if result.get("ok") else (409 if result.get("error_code") == "conflict" else 200)
    return web.json_response(result, status=status)


async def handle_logs(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    name = request.match_info.get("source", "core")
    try:
        n = max(1, min(int(request.query.get("lines", "200")), 2000))
    except ValueError:
        n = 200
    path = _log_path(name)
    if path is None:
        return web.json_response({"error_code": "bad_source",
                                  "message": "未知日志源（core | core_out | snowluma | service）"}, status=404)
    lines: list[str] = []
    if path.exists():
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256_000))
            lines = f.read().decode("utf-8", errors="replace").splitlines()[-n:]
    return web.json_response({"name": name, "file": str(path), "lines": lines})


def _log_path(name: str) -> Path | None:
    today = time.strftime("%Y-%m-%d")
    if name == "core":
        return log_root() / f"app_{today}.log"
    if name == "core_out":
        return log_root() / "manager" / "core.out.log"
    if name == "ingress":
        return log_root() / "manager" / "ingress.out.log"
    if name == "snowluma":
        return log_root() / "manager" / "snowluma.out.log"
    if name == "service":
        return log_root() / "manager" / "service.out.log"
    return None


# ============================================================
# 配置读写（白名单字段，原子写入 .env.dev）
# ============================================================

_CONFIG_ALLOW = {
    "MASTER_ID": str,
    "MASTER_NAME": str,
    "ASSISTANT_QQ": str,
    "ASSISTANT_NAME": str,
    "ASSISTANT_NICK_NAME": str,
    # 接入 / Web 部署字段
    "WEB_WS_PORT": int,
    "ONEBOT_WS_PORT": int,
    "ONEBOT_WS_PATH": str,
    "ONEBOT_ACCESS_TOKEN": str,
    "INGRESS_API_PORT": int,
    "ONEBOT_ENABLED": bool,
    "BRIDGE_ENABLED": bool,
    "BRIDGE_CONNECTION_ID": str,
    "BRIDGE_BASE_URL": str,
    "BRIDGE_TOKEN": str,
    # LLM 接入配置（按四个功能域：common / conversation / vision / embed）
    #   common       FLASH_API_KEY / FLASH_BASE_URL / LLM_FLASH（persona 折入本组）
    #   conversation CONVERSATION_API_KEY / CONVERSATION_BASE_URL / LLM_CONVERSATION（social_chat 同等级）
    #   vision       VISION_API_KEY / VISION_BASE_URL / LLM_VISION
    #   embed        VECTOR_API_KEY / VECTOR_BASE_URL / LLM_EMBEDDING
    "FLASH_API_KEY": str,
    "FLASH_BASE_URL": str,
    "LLM_FLASH": str,
    "LLM_PRO": str,
    "CONVERSATION_API_KEY": str,
    "CONVERSATION_BASE_URL": str,
    "LLM_CONVERSATION": str,
    "PERSONA_API_KEY": str,
    "PERSONA_BASE_URL": str,
    "LLM_PERSONA": str,
    "VISION_API_KEY": str,
    "VISION_BASE_URL": str,
    "LLM_VISION": str,
    "VECTOR_API_KEY": str,
    "VECTOR_BASE_URL": str,
    "LLM_EMBEDDING": str,
}

# 密钥类字段：GET 只回「是否已设置」，不回传明文（PUT 侧语义不受影响）
_SECRET_KEYS = frozenset({
    "BRIDGE_TOKEN",
    "ONEBOT_ACCESS_TOKEN",
    "FLASH_API_KEY",
    "CONVERSATION_API_KEY",
    "PERSONA_API_KEY",
    "VISION_API_KEY",
    "VECTOR_API_KEY",
})
_MAX_VALUE_LEN = 512
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,128}$")
_CONTROL_CHARS = frozenset(chr(i) for i in range(0x20)) | {"\x7f"}


def _env_path() -> Path:
    return data_root() / ".env.dev"


def _read_env_map() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        for line in _env_path().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    except OSError:
        pass
    return result


# ------------------------------------------------------------
# 已保存配置的解析预览（重启 Core 后应用）
# 常驻管理进程可能持有旧 dotenv 值，预览以文件为准；密钥只返回布尔位及来源。
# 这里不声称能读取运行中 Core 的客户端配置。

def _proc_env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def _configured(key: str, env_map: dict[str, str]) -> bool:
    return bool(_effective_value(key, env_map, ""))


def _effective_value(key: str, env_map: dict[str, str], literal_default: str) -> str:
    """文件优先的已保存配置预览，避免常驻进程旧 dotenv 值遮蔽新保存值。"""
    value = env_map[key] if key in env_map else _proc_env(key)
    return (value or "").strip() or literal_default


def _source_label(configured: bool, fallback: bool, builtin: bool = False) -> str:
    if configured:
        return "env"
    if fallback:
        return "fallback"
    if builtin:
        return "builtin"
    return "none"


def _field_source(key: str, env_map: dict[str, str], default: str) -> str:
    """单个字段的来源：已落盘/在环境里 = env，否则内置默认 = builtin。"""
    if _configured(key, env_map):
        return "env"
    return "builtin" if default else "none"


def _snapshot_entry(base_url: str, model: str, key_set: bool, key_source: str,
                    base_url_source: str, model_source: str, note: str = "") -> dict:
    return {
        "base_url": base_url,
        "provider": resolve_provider_label(base_url),
        "model": model,
        "key_set": key_set,
        "key_source": key_source,
        "base_url_source": base_url_source,
        "model_source": model_source,
        "note": note,
    }


def _effective_snapshot(env_map: dict[str, str]) -> dict:
    """已保存配置的解析预览；Core 重启后应用，不冒充运行中 Core 的实时状态。"""
    value = lambda key, default="": _effective_value(key, env_map, default)
    common_key = value("FLASH_API_KEY") or value("OPENCODE_API_KEY")
    common_url = value("FLASH_BASE_URL", DEFAULT_COMMON_URL)
    common_model = value("LLM_FLASH", DEFAULT_COMMON_MODEL)
    common = _snapshot_entry(
        common_url, common_model, bool(common_key),
        _source_label(_configured("FLASH_API_KEY", env_map), bool(value("OPENCODE_API_KEY"))),
        _field_source("FLASH_BASE_URL", env_map, DEFAULT_COMMON_URL),
        _field_source("LLM_FLASH", env_map, DEFAULT_COMMON_MODEL),
    )

    def optional(prefix: str, model_key: str) -> dict:
        own_key = value(f"{prefix}_API_KEY") if prefix else ""
        own_url = value(f"{prefix}_BASE_URL") if prefix else ""
        model = value(model_key)
        return _snapshot_entry(
            own_url or common_url, model or common_model, bool(own_key or common_key),
            "env" if own_key else "fallback",
            "env" if own_url else "fallback",
            "env" if model else "fallback",
            "未填写的字段自动使用 common",
        )

    embed_key = value("VECTOR_API_KEY")
    embed_url = value("VECTOR_BASE_URL")
    return {
        "common": common,
        "conversation": optional("CONVERSATION", "LLM_CONVERSATION"),
        "persona": optional("PERSONA", "LLM_PERSONA"),
        "vision": optional("VISION", "LLM_VISION"),
        "pro": optional("", "LLM_PRO"),
        "embed": _snapshot_entry(
            embed_url or common_url, value("LLM_EMBEDDING", DEFAULT_EMBEDDING_MODEL),
            bool(embed_key or common_key), "env" if embed_key else "fallback",
            "env" if embed_url else "fallback",
            _field_source("LLM_EMBEDDING", env_map, DEFAULT_EMBEDDING_MODEL),
        ),
    }


def _validate_config_value(key: str, value) -> str:
    """校验并规范化单个配置值；非法直接抛 ValueError（调用方转 400）。

    .env.dev 是逐行 `KEY=VALUE` 文本，自由文本字段必须拒绝换行与控制符，
    否则会被拼成注入行。返回可直接落盘的安全文本。
    """
    if _CONFIG_ALLOW[key] is bool:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower()
        raise ValueError(f"{key} 必须为 true 或 false")
    if _CONFIG_ALLOW[key] is int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key}={value!r} 不是合法端口")
        if not 1 <= number <= 65535:
            raise ValueError(f"{key}={value!r} 不是合法端口")
        return str(number)

    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是字符串")
    text = value.strip()
    if len(text) > _MAX_VALUE_LEN:
        raise ValueError(f"{key} 长度超过 {_MAX_VALUE_LEN}")
    if any(ch in _CONTROL_CHARS for ch in text):
        raise ValueError(f"{key} 含换行/控制字符")
    if any(ch.isspace() for ch in text) and key not in ("MASTER_NAME", "ASSISTANT_NAME", "ASSISTANT_NICK_NAME"):
        raise ValueError(f"{key} 不能含空白")
    if "#" in text or '"' in text or "'" in text:
        raise ValueError(f"{key} 不能含 # 或引号")
    if key in ("MASTER_ID", "ASSISTANT_QQ") and text and (not text.isascii() or not text.isdecimal() or int(text) <= 0):
        raise ValueError(f"{key} 必须为有效的账号数字")

    if key == "BRIDGE_CONNECTION_ID":
        from adapt.bridge.contract import valid_connection_id
        valid_connection_id(text)
    if key == "BRIDGE_BASE_URL":
        from adapt.bridge.config import BridgeConfig
        BridgeConfig.from_values({"BRIDGE_BASE_URL": text})
    if key == "ONEBOT_WS_PATH":
        if text and not text.startswith("/"):
            raise ValueError("ONEBOT_WS_PATH 必须以 / 开头")
    elif key.endswith("_BASE_URL"):
        if text and not text.startswith(("http://", "https://")):
            raise ValueError(f"{key} 必须以 http:// 或 https:// 开头")
    elif key.startswith("LLM_"):
        if text and not _MODEL_NAME_RE.match(text):
            raise ValueError(f"{key}={text!r} 不是合法的模型名")
    return text


async def handle_config_get(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    env = _read_env_map()
    fields = {}
    for key, cast in _CONFIG_ALLOW.items():
        raw = env.get(key, "")
        try:
            if cast is bool:
                fields[key] = raw.lower() in ("true", "1", "yes", "on") if raw else key == "ONEBOT_ENABLED"
            else:
                fields[key] = cast(raw) if raw else (0 if cast is int else "")
        except (TypeError, ValueError):
            fields[key] = raw
    # 掩码：密钥类字段只返回是否已设置，不回传明文
    for key in _SECRET_KEYS:
        fields[f"{key}_SET"] = bool(fields.get(key))
        fields.pop(key, None)
    return web.json_response({
        "file": str(_env_path()),
        "fields": fields,
        # 已保存配置的解析预览；重启 Core 后应用
        "effective": _effective_snapshot(env),
        "effective_scope": "saved",
        "defaults": {
            "common": {"base_url": DEFAULT_COMMON_URL, "model": DEFAULT_COMMON_MODEL},
            "embed": {"model": DEFAULT_EMBEDDING_MODEL},
        },
    })


async def handle_config_put(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error_code": "bad_json", "message": "请求体不是 JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error_code": "bad_fields", "message": "请求体必须是对象"}, status=400)
    updates = body.get("fields", {})
    if not isinstance(updates, dict):
        return web.json_response({"error_code": "bad_fields", "message": "fields 必须是对象"}, status=400)
    unknown = [k for k in updates if k not in _CONFIG_ALLOW]
    if unknown:
        return web.json_response({"error_code": "unknown_field",
                                  "message": f"不允许的字段: {', '.join(unknown)}"}, status=400)
    # 校验并规范化（非法值一律 400，不写盘）
    normalized: dict[str, str] = {}
    for key, value in updates.items():
        try:
            normalized[key] = _validate_config_value(key, value)
        except ValueError as error:
            return web.json_response({"error_code": "bad_value", "message": str(error)}, status=400)
    if any(key.startswith("BRIDGE_") for key in normalized):
        from adapt.bridge.config import BridgeConfig
        try:
            BridgeConfig.from_values({**_read_env_map(), **normalized})
        except ValueError as error:
            return web.json_response({"error_code": "bad_value", "message": str(error)}, status=400)
    # 原子写入：读原文件 → 替换/追加白名单键 → 写临时文件 → 替换
    env_path = _env_path()
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for key, value in normalized.items():
        marker = f"{key}="
        idx = next((i for i, l in enumerate(lines) if l.strip().startswith(marker)), None)
        if idx is None:
            lines.append(f"{key}={value}")
        else:
            lines[idx] = f"{key}={value}"
    tmp = env_path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(env_path)
    return web.json_response({"saved": True, "file": str(env_path),
                              "effective": _effective_snapshot(_read_env_map()),
                              "effective_scope": "saved", "requires_restart": True,
                              "note": "已保存，待应用——端口/Web 配置重启对应组件生效；LLM 接入配置（key/url/模型名）需重启 Core 生效"})


_connection_apply = {"running": False, "phase": "", "error": ""}
_connection_apply_task = None


async def handle_connections(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    from ingress_config import api_port
    result = {"available": False, "onebot_enabled": False, "onebot_connected": False,
              "bridge": {"enabled": False}, "apply": dict(_connection_apply)}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
            async with session.get(f"http://127.0.0.1:{api_port()}/v1/status",
                                   headers={"X-Manager-Token": TOKEN}, allow_redirects=False) as response:
                if response.status == 200:
                    body = await response.json()
                    if body.get("service") == "abyss-ingress" and body.get("instance") == str(data_root()):
                        result.update(available=True, onebot_enabled=body.get("onebot_enabled", False),
                                      onebot_connected=body.get("onebot_connected", False),
                                      bridge=body.get("bridge", {"enabled": False}))
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        pass
    return web.json_response(result)


async def handle_connection_check(request: web.Request) -> web.Response:
    """A human-triggered health-only probe. It never sends messages or saves draft settings."""
    if not _authorized(request):
        return _denied()
    from adapt.bridge.config import BridgeConfig
    from adapt.bridge.contract import validate_health
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) - {"BRIDGE_BASE_URL", "BRIDGE_TOKEN"}:
            raise ValueError("检查连接仅接受桥接地址和令牌")
        values = {key: _effective_value(key, _read_env_map(), "")
                  for key in ("BRIDGE_BASE_URL", "BRIDGE_TOKEN", "BRIDGE_CONNECTION_ID")}
        for key, value in body.items():
            values[key] = _validate_config_value(key, value)
        values["BRIDGE_ENABLED"] = "true"
        config = BridgeConfig.from_values(values)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(config.base_url.rstrip("/") + "/health",
                                   headers={"Authorization": f"Bearer {config.token}"},
                                   allow_redirects=False) as response:
                if response.status in (401, 403):
                    return web.json_response({"ok": False, "message": "访问令牌不匹配"}, status=400)
                if response.status != 200:
                    return web.json_response({"ok": False, "message": f"桥接服务返回 HTTP {response.status}"}, status=400)
                health = validate_health(await response.json(), config.platform)
        return web.json_response({"ok": True, "message": "连接与协议检查通过，未发送消息",
                                  "platform": health["platform"], "protocol_version": health["protocol_version"]})
    except ValueError as error:
        return web.json_response({"ok": False, "message": str(error)}, status=400)
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
        return web.json_response({"ok": False, "message": "无法连接桥接服务，请检查地址和服务状态"}, status=503)


async def handle_connections_apply(request: web.Request) -> web.Response:
    """Respond before Core is stopped; the resident manager owns the ordered restart."""
    global _connection_apply_task
    if not _authorized(request):
        return _denied()
    if _connection_apply["running"]:
        return web.json_response({"ok": False, "message": "接入配置正在应用"}, status=409)
    guard = getattr(_service, "update_guard", None)
    if guard and any(guard(name) for name in ("core", "ingress")):
        return web.json_response({"ok": False, "message": "组件正在更新，请完成后再应用配置"}, status=409)
    if _service is None:
        return web.json_response({"ok": False, "message": "管理服务尚未就绪"}, status=503)
    _connection_apply.update(running=True, phase="等待重启", error="")

    async def apply_saved():
        try:
            await asyncio.sleep(0.5)
            for name, action, label in (("core", "stop", "停止 Core"),
                                        ("ingress", "restart", "重启消息接入"),
                                        ("core", "start", "启动 Core")):
                _connection_apply["phase"] = label
                result = await asyncio.to_thread(_service.controller(name).submit, action)
                if not result.get("ok") and result.get("error_code") != "not_running":
                    raise RuntimeError(result.get("message") or label + "失败")
            _connection_apply["phase"] = "已应用"
        except Exception as error:
            _connection_apply.update(phase="应用失败", error=str(error))
        finally:
            _connection_apply["running"] = False

    _connection_apply_task = asyncio.create_task(apply_saved(), name="apply-connections")
    return web.json_response({"ok": True, "applying": True})


async def handle_shutdown(request: web.Request) -> web.Response:
    if not _authorized(request):
        return _denied()
    guard = getattr(_service, "update_guard", None)
    if guard and any(guard(name) for name in ("core", "snowluma", "ingress", "manager", "launcher")):
        return web.json_response({"ok": False, "error_code": "updating", "message": "组件正在安装，请完成或恢复更新后再退出"}, status=409)
    msgs = await _service.stop_everything_async()
    # 受管目标已按优雅流程停止；服务自身无持久状态，延迟自退（给响应留出送达时间）
    asyncio.get_event_loop().call_later(1.0, lambda: os._exit(0))
    return web.json_response({"ok": True, "shutting_down": True, "stopped": msgs})


# ============================================================
# 入口
# ============================================================

def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/info", handle_info)
    app.router.add_get("/v1/targets", handle_targets)
    app.router.add_post("/v1/targets/{target}/{action}", handle_target_cmd)
    app.router.add_get("/v1/logs/{source}", handle_logs)
    app.router.add_get("/v1/config/core", handle_config_get)
    app.router.add_put("/v1/config/core", handle_config_put)
    app.router.add_get("/v1/connections", handle_connections)
    app.router.add_post("/v1/connections/check", handle_connection_check)
    app.router.add_post("/v1/connections/apply", handle_connections_apply)
    app.router.add_post("/v1/shutdown", handle_shutdown)
    from manager_updates import Updates, register_updates
    register_updates(app, Updates(_service, TOKEN, f"http://127.0.0.1:{API_PORT}"), _authorized)
    from manager_console import register_console
    register_console(app, _service, TOKEN, config_handlers=(handle_config_get, handle_config_put),
                     connection_handlers={"/system/connections": handle_connections,
                                          "/system/connections/check": handle_connection_check,
                                          "/system/connections/apply": handle_connections_apply})
    return app


def main() -> int:
    global API_PORT
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="Abyss 管理服务（无界面常驻）")
    parser.add_argument("--port", type=int, default=API_PORT)
    args = parser.parse_args()
    API_PORT = args.port

    # 无控制台启动时自行持有日志文件，窗口关闭不影响服务输出。
    if sys.stdout is None or not sys.stdout.isatty():
        from utils.logger import rotate_log_file

        output = log_root() / "manager" / "service.out.log"
        output.parent.mkdir(parents=True, exist_ok=True)
        rotate_log_file(output)   # 重启先翻页，避免历史输出无限累积
        stream = output.open("a", encoding="utf-8", errors="replace", buffering=1)
        sys.stdout = stream
        sys.stderr = stream

    global _service, _started_at
    from aiohttp import web as _web
    _service = SupervisorService()
    _started_at = time.time()
    app = build_app()
    print(f"[manager-service] v{SERVICE_VERSION} listening on 127.0.0.1:{args.port} "
          f"(data={data_root()})", flush=True)
    # access_log=None：关闭逐请求访问日志，否则自身 API 调用会刷屏 service.out.log
    _web.run_app(app, host="127.0.0.1", port=args.port, print=None,
                 access_log=None, handle_signals=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
