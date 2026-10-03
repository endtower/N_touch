# -*- coding: utf-8 -*-
"""
adapt.web.channel
=================

WebChannel：用浏览器 WebSocket 替代 NapCat/QQ 作为消息通道。

数据源：adapt/web/db_store.py
    会话与消息统一存 SQLite chat_history.db（与主系统同一数据源）
    - 列会话 / 拉历史 / 删消息 / 建删会话 全部走 MessageRepository
    - 用户消息入站 / LLM 回复落库 本来就写同一 DB

协议（JSON over WebSocket）：
    浏览器 -> 服务端:
        {"type": "ping"}
        {"type": "list_sessions"}
        {"type": "create_session", "scope": "private|group",
         "user_id": "...", "user_name": "...", "session_name": "...",
         "role": "..."}
        {"type": "delete_session", "session_id": "..."}
        {"type": "get_messages", "session_id": "..."}
        {"type": "delete_message", "session_id": "...", "message_id": "..."}
        {"type": "delete_messages", "session_id": "...", "message_ids": ["...", ...]}
        {"type": "message", "session_id": "...", "text": "...", "images": [data:...]}

    服务端 -> 浏览器:
        {"type": "hello", "sessions": [...]}
        {"type": "pong", ...}
        {"type": "session_created", "session": {...}}
        {"type": "session_deleted", "session_id": "..."}
        {"type": "sessions", "sessions": [...]}
        {"type": "messages", "session_id": "...", "messages": [...]}
        {"type": "user_echo", ...}
        {"type": "llm_message", ...}
        {"type": "error", ...}
"""

from __future__ import annotations
from paths import data_root

import asyncio
import base64
import binascii
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from aiohttp import WSCloseCode, WSMsgType, web

from utils import setup_logging, NetworkError
from utils.logger import register_frontend_notifier, unregister_frontend_notifier

from adapt.protocol import (
    MessageSegment,
    NormalizedEvent,
    OutboundPayload,
    OutboundTarget,
    Platform,
    SessionScope,
)
from .config import WebChannelConfig
from .models import WebSession
from .db_store import DBStore
from .identity_store import IdentityStore
from .identity_manager import IdentityManager, IdentityError


logger = setup_logging("adapt.web")

# 运行时状态文件位置: adapt/web/data/web_runtime.json
_RUNTIME_DIR = data_root() / "adapt" / "web" / "data"
_RUNTIME_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# 工具
# ============================================================

_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<data>.+)$", re.IGNORECASE)
_EXT_FROM_MIME = {
    "image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp", "image/bmp": "bmp",
    "image/svg+xml": "svg",
}


def _decode_data_uri(uri: str) -> tuple[bytes, str]:
    m = _DATA_URI_RE.match(uri)
    if not m:
        raise ValueError(f"不是合法 data URI: {uri[:60]!r}")
    mime = m.group("mime").lower()
    ext = _EXT_FROM_MIME.get(mime, mime.rsplit("/", 1)[-1] or "jpg")
    try:
        raw = base64.b64decode(m.group("data"), validate=False)
    except binascii.Error as e:
        raise ValueError(f"base64 解码失败: {e}") from e
    return raw, ext


def _segment_to_dict(seg: MessageSegment) -> Dict[str, Any]:
    d = {"type": seg.type}
    if seg.type == "text":
        d["text"] = seg.data.get("text", "")
    elif seg.type == "image":
        d["file"] = seg.data.get("file", "")
        d["url"] = seg.data.get("url", "")
        d["summary"] = seg.data.get("summary", "")
    elif seg.type == "at":
        d["qq"] = seg.data.get("qq", "")
        d["name"] = seg.data.get("name", "")
    else:
        d["data"] = dict(seg.data)
    return d


def _normalize_filename(f: str) -> str:
    """绝对路径 / 带目录 -> basename。"""
    if not f:
        return ""
    f = f.replace("\\", "/")
    if "/" in f:
        f = f.rsplit("/", 1)[-1]
    return f


def _resolve_image_url(web_channel: "WebChannel", file_field: str) -> Optional[str]:
    """从 chat_history 文件里的 file 解析出能用的 URL path, 前端可拼接 base 后 <img src=...>。

    支持的输入:
      1) basename: "4B83...jpg" → 在 web_channel 已知根下递归找
      2) 相对路径: "memory/chat_history/media/images/2026-06-22/xxx.jpg" → 解析到对应根
      3) 绝对路径: "E:\\...\\memory\\llm_save\\stickers\\xxx.jpg" → 解析到 llm_save 根
        (LLM 自存表情包用绝对路径写到 chat_history 的 segment.data.file 里)

    返回的 URL prefix:
      - 在 media_root 下:   /media/<rel>
      - 在 llm_save 下:     /llm-save/<rel>
    """
    if not file_field:
        return None
    base = _normalize_filename(file_field)
    if not base:
        return None

    # 已经是 http(s)/data/已带前端的 URL / 前端能解析的形式: 直接返回原样 (返回 file_field 让前端沿用)
    if file_field.startswith(("http://", "https://", "data:", "/")):
        return file_field if file_field.startswith("/") else None

    media_root = web_channel._media_root.resolve()
    llm_save_root = web_channel._llm_save_root.resolve()

    # ---- 路径 a: 绝对路径直接定位 ----
    if file_field and (
        file_field[1:3] == ":\\"            # Windows "E:\..."
        or file_field.startswith("/")       # Unix /...
        or file_field.startswith("\\\\")    # UNC
    ):
        try:
            abs_path = Path(file_field).resolve()
        except OSError:
            abs_path = None
        if abs_path is not None and abs_path.is_file():
            # 在 media_root 下?
            try:
                rel = abs_path.relative_to(media_root)
                return f"/media/{rel.as_posix()}"
            except ValueError:
                pass
            # 在 llm_save_root 下?
            try:
                rel = abs_path.relative_to(llm_save_root)
                return f"/llm-save/{rel.as_posix()}"
            except ValueError:
                pass

    # ---- 路径 b: 相对路径 (形如 "memory/chat_history/media/images/xxx.jpg") ----
    # 也走 prefix 映射, 因为 LLM 写出来的可能是这样的相对路径.
    if file_field:
        try:
            rel_path = Path(file_field.replace("\\", "/")).resolve()
            if rel_path.is_file():
                try:
                    rel = rel_path.relative_to(media_root)
                    return f"/media/{rel.as_posix()}"
                except ValueError:
                    pass
                try:
                    rel = rel_path.relative_to(llm_save_root)
                    return f"/llm-save/{rel.as_posix()}"
                except ValueError:
                    pass
        except OSError:
            pass

    # ---- 路径 c: 文件已经以 basename "xxx.jpg" 形式过来, 在两个根下递归找 ----
    direct = (media_root / base).resolve()
    try:
        direct.relative_to(media_root)
    except ValueError:
        direct = None
    if direct is not None and direct.is_file():
        return f"/media/{base}"

    # media_root 递归找（images/ 优先）
    found: Optional[Path] = None
    try:
        for p in media_root.rglob(base):
            if p.is_file():
                rel = p.relative_to(media_root).as_posix()
                if rel.startswith("images/"):
                    found = p
                    break
                if found is None:
                    found = p
    except OSError:
        return None
    if found is not None:
        rel = found.relative_to(media_root).as_posix()
        return f"/media/{rel}"

    # llm_save_root 递归找
    if llm_save_root.is_dir():
        try:
            for p in llm_save_root.rglob(base):
                if p.is_file():
                    rel = p.relative_to(llm_save_root).as_posix()
                    return f"/llm-save/{rel}"
        except OSError:
            pass

    return None


def _file_msg_to_dict(m: Dict[str, Any], web_channel: "WebChannel") -> Dict[str, Any]:
    """chat_history 里的一条 message -> 前端可用的扁平 dict。

    返回的 images 是带 /media/ 前缀的完整 URL 列表, 前端直接 <img src=...> 就行。
    """
    content = m.get("content") or []
    text = m.get("content_text") or ""
    images: List[str] = []
    other_segments: List[Dict[str, Any]] = []
    for seg in content:
        if not isinstance(seg, dict):
            continue
        st = seg.get("type")
        sd = seg.get("data") or {}
        if st == "image":
            f = sd.get("file")
            url = _resolve_image_url(web_channel, f) if f else None
            if url:
                images.append(url)
            else:
                # 解析不到就保留 basename, 前端会显示占位符
                bn = _normalize_filename(f) if f else ""
                if bn:
                    images.append(f"/media/{bn}?unresolved=1")
    return {
        "message_id": m.get("message_id"),
        "user_id": m.get("user_id"),
        "user_name": m.get("user_name"),
        "timestamp": m.get("timestamp"),
        "content_text": text,
        "content": content,
        "images": images,
    }


# ============================================================
# WebChannel
# ============================================================

class WebChannel:
    """WebSocket 服务端 Channel。"""

    name: str
    platform: Platform = Platform.WEB

    def __init__(self, config: WebChannelConfig):
        self.config = config
        self.name = config.name

        # 网络层
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None

        # 浏览器连接
        self._clients: Set[web.WebSocketResponse] = set()
        self._clients_lock = asyncio.Lock()

        # 消息处理器（外部注入，入站消息直达 message_handler）
        self._message_processor: Optional[Any] = None

        # 会话存储（SQLite chat_history.db = 与主系统同一数据源）
        self._store = DBStore()

        # 媒体目录
        self._media_root: Path = Path(config.media_base_dir)
        self._media_root.mkdir(parents=True, exist_ok=True)

        # LLM 自存媒体目录 (memory/llm_save/)，如 stickers/xxx.jpg、images/xxx.png
        self._llm_save_root: Path = self._media_root.parent.parent / "llm_save"
        self._llm_save_root.mkdir(parents=True, exist_ok=True)

        # 身份运行时存储 (路径: adapt/web/data/web_runtime.json)
        self._identity_store = IdentityStore(data_dir=_RUNTIME_DIR)
        self._identity_manager = IdentityManager(self._identity_store)

    # --------------------------------------------------------
    # 注入
    # --------------------------------------------------------

    def bind_message_processor(self, message_processor: Any) -> None:
        self._message_processor = message_processor

    def is_connected(self) -> bool:
        return len(self._clients) > 0

    # --------------------------------------------------------
    # 会话操作（走 store）
    # --------------------------------------------------------

    async def _list_sessions(self) -> List[WebSession]:
        return await self._store.list_sessions()

    async def _get_session(self, session_id: str, scope: Optional[str] = None) -> Optional[WebSession]:
        """按 session_id（裸）找会话。如果传了 scope 就用 (scope, session_id)，否则扫所有 scope。"""
        for s in await self._store.list_sessions():
            if s.session_id == session_id:
                if scope is None or s.scope == scope:
                    return s
        return None

    # --------------------------------------------------------
    # 生命周期
    # --------------------------------------------------------

    async def start(self) -> None:
        if self._site is not None:
            return
        self._app = web.Application()
        self._app.router.add_get(self.config.path, self._handle_connection)
        self._app.router.add_get("/media/{tail:.*}", self._handle_media)
        # LLM 自存表情包 / 图片: memory/llm_save/{stickers,images}/...
        self._app.router.add_get("/llm-save/{tail:.*}", self._handle_llm_save_media)
        self._app.router.add_get("/health", self._handle_health)
        self._app.router.add_get("/sessions", self._handle_list_sessions_http)
        self._app.router.add_get("/messages", self._handle_list_messages_http)  # ?scope=&id=

        # 记忆社交实体可视化 (只读) + 长期事件列表
        from .memory_api import (
            handle_get_social_memory,
            handle_get_experiences,
            handle_update_social_memory,
            handle_update_experience,
            handle_delete_experience,
        )
        from .graph_api import (
            handle_list_tasks,
            handle_set_task_status,
            handle_update_task,
            handle_create_task,
            handle_delete_task,
        )
        # 上下文查看 (只读, 不改 memory/)
        from .context_api import handle_chains, handle_read_file
        # Token 用量统计 (只读, 按天 / agent / provider / model)
        from .token_api import handle_token_stats
        # Amy 情绪系统状态 (只读) + 想法池调试编辑 (增/删/改)
        from .amy_api import (
            handle_get_amy_state,
            handle_create_amy_thought,
            handle_apply_amy_final_evaluation,
            handle_update_amy_thought,
            handle_delete_amy_thought,
        )
        # 记事本（notepad）状态查看 (只读)
        from .notes_api import handle_get_notes
        # LLM 自存表情包管理 (list/update/delete) + 一键向量化
        from .sticker_api import (
            handle_list_stickers,
            handle_update_sticker,
            handle_delete_sticker,
            handle_vectorize_stickers,
        )
        # 系统管理接口 (管家增量②): 状态 / SnowLuma 启停 / Core 关闭重启 / 日志 tail
        # 注意: 必须注册在前端 dist 兜底路由之前, 否则会被 /{tail} 抢走
        from .system_api import (
            handle_system_status,
            handle_snowluma_start,
            handle_snowluma_stop,
            handle_core_shutdown,
            handle_core_restart,
            handle_system_logs,
            handle_logs_ws,
            handle_config_get,
            handle_config_put,
        )
        self._app.router.add_get("/system/status", handle_system_status)
        self._app.router.add_post("/system/snowluma/start", handle_snowluma_start)
        self._app.router.add_post("/system/snowluma/stop", handle_snowluma_stop)
        self._app.router.add_post("/system/core/shutdown", handle_core_shutdown)
        self._app.router.add_post("/system/core/restart", handle_core_restart)
        self._app.router.add_get("/system/logs", handle_system_logs)
        self._app.router.add_get("/system/ws/logs", handle_logs_ws)
        self._app.router.add_get("/system/config", handle_config_get)
        self._app.router.add_put("/system/config", handle_config_put)

        # 记忆社交实体: 以 00000000 为中心的星形图 (只读)
        self._app.router.add_get("/memory/social", handle_get_social_memory)
        # 记忆内容编辑: PATCH 修改角色/群组/模糊角色的具体字段
        self._app.router.add_patch("/memory/social/{entity_type}/{entity_id}", handle_update_social_memory)
        # 记忆经验/嘱托: bequests 表 (只读 + 内联编辑 + 删除)
        self._app.router.add_get("/memory/experiences", handle_get_experiences)
        self._app.router.add_patch("/memory/experiences/{bequest_id}", handle_update_experience)
        self._app.router.add_delete("/memory/experiences/{bequest_id}", handle_delete_experience)
        # 任务列表 (只读 + 状态快改 + 字段编辑 + 创建 + 删除；URL 暂沿用 /events/*)
        self._app.router.add_get("/events/list", handle_list_tasks)
        self._app.router.add_post("/events/set_type", handle_set_task_status)
        self._app.router.add_post("/events", handle_create_task)
        self._app.router.add_patch("/events/{event_id}", handle_update_task)
        self._app.router.add_delete("/events/{event_id}", handle_delete_task)

        # 上下文查看 (只读, 不改 memory/)
        self._app.router.add_get("/context/chains", handle_chains)
        self._app.router.add_get("/context/file", handle_read_file)

        # Token 用量统计 (只读, 按天 / agent / provider / model)
        self._app.router.add_get("/token-stats", handle_token_stats)

        # Amy 情绪系统状态 (只读) + 想法池调试编辑 (增/删/改)
        self._app.router.add_get("/amy/state", handle_get_amy_state)
        self._app.router.add_post("/amy/thoughts", handle_create_amy_thought)
        self._app.router.add_post("/amy/final-evaluation", handle_apply_amy_final_evaluation)
        self._app.router.add_patch("/amy/thoughts/{thought_id}", handle_update_amy_thought)
        self._app.router.add_delete("/amy/thoughts/{thought_id}", handle_delete_amy_thought)

        # 记事本（notepad）状态查看 (只读)
        self._app.router.add_get("/notes", handle_get_notes)

        # LLM 自存表情包管理 (list/update/delete) + 一键向量化
        self._app.router.add_get("/stickers/list", handle_list_stickers)
        self._app.router.add_post("/stickers/vectorize", handle_vectorize_stickers)
        self._app.router.add_patch("/stickers/{filename}", handle_update_sticker)
        self._app.router.add_delete("/stickers/{filename}", handle_delete_sticker)

        # 前端构建产物伺服 (web_frontend/dist 存在才注册): 5002 直接提供页面 + API,
        # 生产模式不再依赖 Vite dev server。静态路由必须注册在所有 API 路由之后,
        # 兜底规则才不会抢走 /memory /events 等已有路径。
        self._dist_dir = Path(__file__).resolve().parents[2] / "web_frontend" / "dist"
        if self._dist_dir.is_dir():
            assets_dir = self._dist_dir / "assets"
            if assets_dir.is_dir():
                self._app.router.add_static("/assets", assets_dir)
            self._app.router.add_get("/", self._handle_index)
            self._app.router.add_get("/{tail:.*}", self._handle_spa_fallback)

        # access_log=None: 关闭 aiohttp 访问日志, 避免高频轮询请求刷屏干扰终端
        self._app["started_at"] = time.time()   # 给 /system/status 算 uptime 用
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await self._site.start()

        # 加载身份运行时 + 自动初始化。
        # —— 端口已对外开放：此后任何初始化失败都必须自清理（GPT 审查-4）——
        # 否则失败通道的监听器残留，端口被占且无人回滚。
        try:
            self._identity_store.load()
            await self._identity_store.ensure_initialized_from_store()

            # 注册前端 warning 通知器: 任意模块的 warning/error/critical 日志会广播到所有 ws 客户端,
            # 前端按 type="warning" 协议弹窗展示.
            self._frontend_notifier = self._make_frontend_notifier()
            register_frontend_notifier(self._frontend_notifier)

            # 加载 truck 状态覆盖 (从磁盘恢复到内存 config 对象)
            try:
                from adapt.web.truck_control import init_from_disk
                init_from_disk()
            except Exception as e:
                logger.warning(f"[Web:{self.name}] 初始化 truck 状态失败: {e}")

            # 注册 GlobalSessionState 变更回调 (LLM 处理中的会话 / 聊天目的)
            # 关键功能: 注册失败直接阻断启动, 防止前端状态同步静默缺失
            from brain.hc.global_session_state import register_state_change_callback
            await register_state_change_callback(self._on_global_state_change)
        except BaseException:
            await self.stop()
            raise

        sessions = await self._list_sessions()
        logger.info(
            f"[Web:{self.name}] WS server listening on "
            f"ws://{self.config.host}:{self.config.port}{self.config.path}"
        )
        logger.info(
            f"[Web:{self.name}] 扫到 {len(sessions)} 个会话文件: "
            f"{[s.session_id for s in sessions[:5]]}{'...' if len(sessions) > 5 else ''}"
        )

    async def stop(self) -> None:
        # 先摘掉 warning 通知器, 避免 stop 流程中残留日志再次触发广播
        ntf = getattr(self, "_frontend_notifier", None)
        if ntf is not None:
            try:
                unregister_frontend_notifier(ntf)
            except Exception:
                logger.debug("[Web] unregister_frontend_notifier 失败", exc_info=True)
            self._frontend_notifier = None

        # 注销 GlobalSessionState 变更回调 (与 start 对称)
        try:
            from brain.hc.global_session_state import unregister_state_change_callback
            await unregister_state_change_callback(self._on_global_state_change)
        except Exception:
            logger.debug("[Web] unregister_state_change_callback 失败", exc_info=True)

        async with self._clients_lock:
            for ws in list(self._clients):
                if not ws.closed:
                    try:
                        await ws.close(code=WSCloseCode.GOING_AWAY)
                    except Exception:
                        pass
            self._clients.clear()

        if self._site is not None:
            try:
                await self._site.stop()
            except Exception:
                logger.debug("[Web] site.stop 失败", exc_info=True)
            self._site = None
        if self._runner is not None:
            try:
                await self._runner.cleanup()
            except Exception:
                logger.debug("[Web] runner.cleanup 失败", exc_info=True)
            self._runner = None
            self._app = None
        logger.info(f"[Web:{self.name}] stopped")

    # --------------------------------------------------------
    # HTTP 路由（调试用）
    # --------------------------------------------------------

    async def _handle_health(self, _request: web.Request) -> web.Response:
        sessions = await self._list_sessions()
        return web.json_response({
            "ok": True,
            "channel": self.name,
            "platform": self.platform.value,
            "clients": len(self._clients),
            "sessions": [s.to_dict() for s in sessions],
        })

    async def _handle_list_sessions_http(self, _request: web.Request) -> web.Response:
        sessions = await self._list_sessions()
        return web.json_response({"sessions": [s.to_dict() for s in sessions]})

    async def _handle_list_messages_http(self, request: web.Request) -> web.Response:
        scope = request.query.get("scope")
        sid = request.query.get("id")
        if not scope or not sid:
            return web.json_response({"error": "scope 和 id 必填"}, status=400)
        msgs = await self._store.load_messages(scope, sid)

        # limit: 只返回最新若干条, 默认返回全部 (0/未传 = 全部)
        raw_limit = request.query.get("limit", "")
        try:
            limit = int(raw_limit) if raw_limit else 0
        except ValueError:
            limit = 0
        total = len(msgs)
        selected = msgs[-limit:] if limit and limit > 0 else msgs
        return web.json_response({
            "session_id": sid,
            "scope": scope,
            "total": total,
            "messages": [_file_msg_to_dict(m, self) for m in selected],
        })

    async def _handle_media(self, request: web.Request) -> web.Response:
        tail = request.match_info.get("tail", "")
        if ".." in tail or tail.startswith("/") or "\\" in tail:
            return web.Response(status=400, text="bad path")
        target = (self._media_root / tail).resolve()
        try:
            target.relative_to(self._media_root.resolve())
        except ValueError:
            return web.Response(status=400, text="bad path")
        if not target.is_file():
            return web.Response(status=404, text="not found")
        return self._serve_file(target)

    async def _handle_llm_save_media(self, request: web.Request) -> web.Response:
        """服务 memory/llm_save/{stickers,images}/... 下的文件 (LLM 自存表情包/图片)。"""
        tail = request.match_info.get("tail", "")
        if ".." in tail or tail.startswith("/") or "\\" in tail:
            return web.Response(status=400, text="bad path")
        target = (self._llm_save_root / tail).resolve()
        try:
            target.relative_to(self._llm_save_root.resolve())
        except ValueError:
            return web.Response(status=400, text="bad path")
        if not target.is_file():
            return web.Response(status=404, text="not found")
        return self._serve_file(target)

    @staticmethod
    def _serve_file(target: Path) -> web.Response:
        ext = target.suffix.lower().lstrip(".")
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif",
            "webp": "image/webp", "bmp": "image/bmp", "svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        return web.Response(body=target.read_bytes(), content_type=mime)

    # --------------------------------------------------------
    # 前端静态文件（web_frontend/dist）
    # --------------------------------------------------------

    def _dist_index(self) -> Optional[Path]:
        index = self._dist_dir / "index.html"
        return index if index.is_file() else None

    async def _handle_index(self, _request: web.Request) -> web.Response:
        index = self._dist_index()
        if index is None:
            return web.Response(status=404, text="dist/index.html 不存在，请先 npm run build")
        return web.FileResponse(index)

    async def _handle_spa_fallback(self, request: web.Request) -> web.Response:
        """兜底路由：先尝试 dist 下的同名文件（favicon 等），否则回 index.html。"""
        index = self._dist_index()
        if index is None:
            return web.Response(status=404, text="dist/index.html 不存在，请先 npm run build")
        tail = request.match_info.get("tail", "")
        if tail:
            target = (self._dist_dir / tail).resolve()
            try:
                target.relative_to(self._dist_dir.resolve())
            except ValueError:
                target = None  # 路径穿越，当作未命中
            if target is not None and target.is_file():
                return web.FileResponse(target)
        return web.FileResponse(index)

    # --------------------------------------------------------
    # WebSocket 接入
    # --------------------------------------------------------

    async def _handle_connection(self, request: web.Request) -> web.WebSocketResponse:
        if not self._check_auth(request):
            logger.warning(f"[Web] 鉴权失败 from {request.remote}")
            return web.Response(status=401, text="unauthorized")

        ws = web.WebSocketResponse(heartbeat=30.0)
        try:
            await ws.prepare(request)
        except Exception:
            logger.exception("[Web] ws.prepare 失败")
            return ws

        async with self._clients_lock:
            self._clients.add(ws)
        logger.info(f"[Web:{self.name}] 浏览器已连接 from {request.remote} (active={len(self._clients)})")

        # 当前 GlobalSessionState 快照 (给新连接的客户端同步状态)
        global_state_snapshot = {"sessions": [], "event_chain_active": False}
        try:
            from brain.hc.global_session_state import get_global_session_state
            state = get_global_session_state()
            global_state_snapshot = {
                "sessions": state.get_sessions(),
                "event_chain_active": getattr(state, "_event_chain_active", False),
            }
        except Exception:
            pass

        try:
            await ws.send_json({
                "type": "hello",
                "channel": self.name,
                "platform": self.platform.value,
                "sessions": [s.to_dict() for s in await self._list_sessions()],
                "runtimes": {
                    sid: rt.to_dict()
                    for sid, rt in self._identity_store.get_all_runtimes().items()
                },
                "global_state": global_state_snapshot,
            })
        except Exception:
            pass

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(msg.data, ws)
                elif msg.type == WSMsgType.BINARY:
                    logger.warning("[Web] 收到 binary 帧，忽略")
                elif msg.type == WSMsgType.ERROR:
                    logger.warning(f"[Web] WS error: {ws.exception()}")
                    break
        except Exception:
            logger.exception("[Web] 读循环异常")
        finally:
            async with self._clients_lock:
                self._clients.discard(ws)
            logger.info(f"[Web:{self.name}] 浏览器已断开 (active={len(self._clients)})")

        return ws

    def _check_auth(self, request: web.Request) -> bool:
        token = self.config.access_token
        if not token:
            return True
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[len("Bearer "):].strip() == token:
            return True
        if request.query.get("access_token") == token:
            return True
        return False

    # --------------------------------------------------------
    # 入站分发
    # --------------------------------------------------------

    async def _on_text(self, raw_text: str, ws: web.WebSocketResponse) -> None:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            await self._send_error(ws, "invalid json")
            return
        if not isinstance(data, dict):
            await self._send_error(ws, "expected object")
            return

        kind = data.get("type")
        try:
            if kind == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
            elif kind == "list_sessions":
                await self._handle_list_sessions(ws)
            elif kind == "create_session":
                await self._handle_create_session(data, ws)
            elif kind == "delete_session":
                await self._handle_delete_session(data, ws)
            elif kind == "get_messages":
                await self._handle_get_messages(data, ws)
            elif kind == "delete_message":
                await self._handle_delete_message(data, ws)
            elif kind == "delete_messages":
                await self._handle_delete_messages(data, ws)
            elif kind == "message":
                await self._handle_message(data, ws)
            elif kind == "create_identity":
                await self._handle_create_identity(data, ws)
            elif kind == "update_identity":
                await self._handle_update_identity(data, ws)
            elif kind == "delete_identity":
                await self._handle_delete_identity(data, ws)
            elif kind == "switch_identity":
                await self._handle_switch_identity(data, ws)
            elif kind == "get_trucks":
                await self._handle_get_trucks(ws)
            elif kind == "set_truck":
                await self._handle_set_truck(data, ws)
            elif kind == "switch_test_mode":
                await self._handle_switch_test_mode(data, ws)
            elif kind == "list_presets":
                await self._handle_list_presets(ws)
            elif kind == "apply_env":
                await self._handle_apply_env(data, ws)
            elif kind == "capture_mirror":
                await self._handle_capture_mirror(data, ws)
            elif kind == "copy_state":
                await self._handle_copy_state(data, ws)
            elif kind == "delete_preset":
                await self._handle_delete_preset(data, ws)
            else:
                await self._send_error(ws, f"unknown type: {kind!r}")
        except Exception as e:
            logger.exception(f"[Web] 处理 {kind} 时异常")
            await self._send_error(ws, f"handler error: {e}")

    async def _handle_list_sessions(self, ws: web.WebSocketResponse) -> None:
        await ws.send_json({
            "type": "sessions",
            "sessions": [s.to_dict() for s in await self._list_sessions()],
        })

    async def _handle_create_session(self, data: Dict, ws: web.WebSocketResponse) -> None:
        scope_raw = (data.get("scope") or "private").strip().lower()
        if scope_raw not in ("private", "group"):
            await self._send_error(ws, f"scope 必须是 private/group")
            return

        user_id = (data.get("user_id") or "").strip()
        if not user_id:
            await self._send_error(ws, "user_id 必填（你的身份 ID）")
            return
        user_name = (data.get("user_name") or user_id).strip()

        # 私聊: bare_id = user_id (会话 ID = 我的 ID)
        # 群聊: bare_id = group_id (群号, 作为会话 ID), 我的身份 ID = user_id (用 RoleConfig.MASTER_NAME)
        if scope_raw == "private":
            bare_id = user_id
            if not user_name:
                user_name = user_id
            session_name = (data.get("session_name") or user_name).strip()
        else:
            group_id = (data.get("group_id") or "").strip()
            if not group_id:
                await self._send_error(ws, "群聊必须给 group_id (QQ 群号)")
                return
            bare_id = group_id
            session_name = (data.get("session_name") or "").strip()
            if not session_name:
                await self._send_error(ws, "群聊必须给会话名 (session_name)")
                return

        # role 已经废弃, 角色由 IdentityBar 管理, 这里忽略前端传的 role
        role = None

        sess = await self._store.create_empty(
            scope=scope_raw,
            bare_id=bare_id,
            user_name=user_name,
            session_name=session_name,
            role=role,
        )

        # 给新建的会话初始化 runtime (如果之前没自动初始化)
        rt = self._identity_store.get_runtime(user_id)
        if rt is None:
            from .models import Identity
            import time as _t
            seed = Identity(
                user_id=user_id,
                user_name=user_name,
                is_default=True,
                created_at=_t.time(),
            )
            from .models import SessionRuntime
            rt = SessionRuntime(
                session_id=user_id,
                scope=scope_raw,
                active_identity=seed,
                identity_lib=[seed],
            )
            self._identity_store.put_runtime(rt)
            self._identity_store.save()
            await self._broadcast_runtime(rt)

        await self._broadcast({"type": "session_created", "session": sess.to_dict()})
        await ws.send_json({
            "type": "sessions",
            "sessions": [s.to_dict() for s in await self._list_sessions()],
        })
        logger.info(
            f"[Web] 创建会话: bare_id={bare_id} scope={scope_raw} "
            f"user=({user_id}, {user_name}) session_name={session_name!r}"
        )

    async def _handle_delete_session(self, data: Dict, ws: web.WebSocketResponse) -> None:
        sid = (data.get("session_id") or "").strip()
        if not sid:
            await self._send_error(ws, "session_id 必填")
            return

        # 找会话
        target = None
        for s in await self._list_sessions():
            if s.session_id == sid:
                target = s
                break
        if target is None:
            await self._send_error(ws, f"会话 {sid} 不存在")
            return

        ok = await self._store.delete_session(target.scope, target.session_id)
        if not ok:
            await self._send_error(ws, f"会话 {sid} 不存在")
            return
        # 同步清理 runtime (避免下次同 id 新建时用到旧的)
        rt = self._identity_store.get_runtime(sid)
        if rt is not None:
            self._identity_store._sessions.pop(sid, None)
            self._identity_store.save()
            logger.info(f"[Web] 清理 runtime: session={sid}")
        await self._broadcast({"type": "session_deleted", "session_id": sid})
        logger.info(f"[Web] 删除会话: {sid}")

    async def _handle_get_messages(self, data: Dict, ws: web.WebSocketResponse) -> None:
        sid = (data.get("session_id") or "").strip()
        if not sid:
            await self._send_error(ws, "session_id 必填")
            return

        # 找这个 session
        sess = await self._get_session(sid)
        if sess is None:
            await self._send_error(ws, f"会话 {sid} 不存在")
            return

        # ---- 分页参数 ----
        # limit   : 最多返回多少条; 默认 50; 上限 200 (防单帧过大)
        # before_id: 只取该 message_id 之前的消息 (不含), 用于上滑加载更多
        #            None 或不传 = 取最后 limit 条
        try:
            limit = int(data.get("limit") or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        before_id = (data.get("before_id") or "").strip() or None

        page = await self._store.get_messages_page(sess.scope, sess.session_id, before_id, limit)
        if before_id is not None and not page.get("found", True):
            # 找不到 before_id (可能已被删), 回退到末尾并提示前端重置
            await self._send_error(ws, f"before_id={before_id} 未找到, 已回退到末尾")

        msgs_out = [_file_msg_to_dict(m, self) for m in page["messages"]]
        await ws.send_json({
            "type": "messages",
            "session_id": sid,
            "scope": sess.scope,
            "messages": msgs_out,
            "total": page.get("total", len(msgs_out)),
            "returned": len(msgs_out),
            "has_more": page["has_more"],
            "oldest_message_id": page["oldest_message_id"],
        })
        logger.debug(
            f"[Web] 拉取消息: session={sid} returned={len(msgs_out)} "
            f"total={page.get('total', len(msgs_out))} has_more={page['has_more']} before_id={before_id}"
        )

    async def _handle_delete_message(self, data: Dict, ws: web.WebSocketResponse) -> None:
        """从前端单条删除某条消息, 同步广播给所有 web 客户端."""
        sid = (data.get("session_id") or "").strip()
        message_id = (data.get("message_id") or "").strip()
        if not sid:
            await self._send_error(ws, "session_id 必填")
            return
        if not message_id:
            await self._send_error(ws, "message_id 必填")
            return

        sess = await self._get_session(sid)
        if sess is None:
            await self._send_error(ws, f"会话 {sid} 不存在")
            return

        ok = await self._store.delete_message(sess.scope, sess.session_id, message_id)
        if not ok:
            await self._send_error(ws, f"消息 {message_id} 未找到")
            return

        # 广播给所有连接的客户端, 同步删除 UI
        await self._broadcast({
            "type": "message_deleted",
            "session_id": sid,
            "message_id": message_id,
        })
        logger.info(f"[Web] 删除消息: session={sid} message_id={message_id}")

    async def _handle_delete_messages(self, data: Dict, ws: web.WebSocketResponse) -> None:
        """从前端批量删除消息（多选/框选）, 同步广播给所有 web 客户端."""
        sid = (data.get("session_id") or "").strip()
        if not sid:
            await self._send_error(ws, "session_id 必填")
            return
        raw_ids = data.get("message_ids")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or not all(isinstance(x, str) and x.strip() for x in raw_ids)
        ):
            await self._send_error(ws, "message_ids 必须为非空字符串数组")
            return
        ids = [x.strip() for x in raw_ids]

        sess = await self._get_session(sid)
        if sess is None:
            await self._send_error(ws, f"会话 {sid} 不存在")
            return

        deleted = await self._store.delete_messages(sess.scope, sess.session_id, ids)
        if not deleted:
            await self._send_error(ws, "所选消息均未找到")
            return

        # 广播给所有连接的客户端, 同步删除 UI (只广播实际删除的 id)
        await self._broadcast({
            "type": "messages_deleted",
            "session_id": sid,
            "message_ids": deleted,
        })
        logger.info(f"[Web] 批量删除消息: session={sid} count={len(deleted)}/{len(ids)}")

    async def _handle_message(self, data: Dict, ws: web.WebSocketResponse) -> None:
        sid = (data.get("session_id") or "").strip()
        if not sid:
            await self._send_error(ws, "session_id 必填")
            return
        sess = await self._get_session(sid)
        if sess is None:
            await self._send_error(ws, f"会话 {sid} 不存在（请先 create_session）")
            return

        # 取当前激活身份 (没 runtime 就报错, 防止空身份发消息)
        runtime = self._identity_store.get_runtime(sid)
        if runtime is None or not runtime.identity_lib:
            await self._send_error(ws, "该会话没有任何可用身份, 无法发消息")
            return
        active = runtime.active_identity

        text = (data.get("text") or "").strip()
        images = data.get("images") or []
        if not isinstance(images, list):
            await self._send_error(ws, "images must be list")
            return
        if not text and not images:
            await self._send_error(ws, "empty message")
            return

        # 1) 图片落盘
        file_segs: List[MessageSegment] = []
        if images:
            if len(images) > self.config.max_images_per_message:
                await self._send_error(ws, f"too many images: {len(images)} > {self.config.max_images_per_message}")
                return
            try:
                for uri in images:
                    if not isinstance(uri, str):
                        raise ValueError("image item must be string")
                    raw, ext = _decode_data_uri(uri)
                    if len(raw) > self.config.max_image_bytes:
                        raise ValueError(f"image too large: {len(raw)} > {self.config.max_image_bytes}")
                    date = datetime.now().strftime("%Y-%m-%d")
                    fname = f"web_{uuid.uuid4().hex}.{ext}"
                    day_dir = self._media_root / "images" / date
                    day_dir.mkdir(parents=True, exist_ok=True)
                    (day_dir / fname).write_bytes(raw)
                    rel = f"images/{date}/{fname}"
                    file_segs.append(MessageSegment(
                        type="image",
                        data={"file": rel, "summary": "[图片]", "url": ""},
                    ))
            except ValueError as e:
                await self._send_error(ws, f"image error: {e}")
                return

        # 2) segments
        segments: List[MessageSegment] = []
        if text:
            segments.append(MessageSegment(type="text", data={"text": text}))
        segments.extend(file_segs)
        content_text = "".join(seg.to_text() for seg in segments)

        # 3) NormalizedEvent —— 用 runtime.active_identity, 不用 sess 自带的
        message_id = uuid.uuid4().hex
        now = int(time.time())
        scope = SessionScope(sess.scope)
        normalized_session_id = f"{sess.scope}_{sid}"
        ev = NormalizedEvent(
            platform=self.platform,
            scope=scope,
            session_id=normalized_session_id,
            # session_name 取身份名（对端真实名字），不用会话持久化名，
            # 避免把 user_id 数字(10000 之类)当作角色名写入记忆别名
            session_name=active.user_name,
            user_id=active.user_id,
            user_name=active.user_name,
            message_id=message_id,
            timestamp=now,
            segments=segments,
            content_text=content_text,
            raw={
                "web": True,
                "text": text,
                "image_count": len(file_segs),
                "role": None,
                "original_session_id": sid,
                "active_identity": active.to_dict(),
            },
        )

        # 4) echo
        await self._broadcast({
            "type": "user_echo",
            "session_id": sid,
            "message_id": message_id,
            "text": text,
            "images": [seg.data.get("file", "") for seg in file_segs],
            "timestamp": now,
            "user_id": active.user_id,
            "user_name": active.user_name,
            "role": None,
        })

        # 5) 入站直达消息处理器
        if self._message_processor is None:
            await self._send_error(ws, "message_processor not bound")
            return
        try:
            payload = ev.to_system_message_dict()
            await self._message_processor.process_message(payload)
            logger.debug(
                f"[Web] dispatched: session={sid} user={sess.user_id} "
                f"role={sess.role!r} text={content_text[:60]!r} images={len(file_segs)}"
            )
        except Exception:
            logger.exception("[Web] 入站消息处理失败")
            await self._send_error(ws, "dispatch failed")

    async def _send_error(self, ws: web.WebSocketResponse, msg: str) -> None:
        try:
            await ws.send_json({"type": "error", "message": msg})
        except Exception:
            pass

    async def _send_warning(self, ws: web.WebSocketResponse, code: str, message: str, session_id: Optional[str] = None) -> None:
        try:
            payload = {"type": "warning", "code": code, "message": message}
            if session_id:
                payload["session_id"] = session_id
            await ws.send_json(payload)
        except Exception:
            pass

    async def _broadcast_runtime(self, runtime: SessionRuntime) -> None:
        """把单个 session 的 runtime 广播给所有连接的浏览器。"""
        await self._broadcast({
            "type": "session_runtime",
            "runtime": runtime.to_dict(),
        })

    async def broadcast_activity(self, activity: Optional[Dict[str, Any]]) -> None:
        """广播后端维护的单一活动状态给所有 web 客户端（前端只渲染）。

        设计：后端（GlobalSessionState.FrontendBroadcastMixin）维护单一 activity 值，
        前端收到 activity 就渲染，收到 None（idle）就清空显示。
        取代早期的多类广播方案（前端自行维护状态）。

        Args:
            activity: 活动状态 dict（source / status / tool_name / args_summary /
                parent_tool_name / timestamp）；None 表示空闲（事件链结束）。
        """
        out = {
            "type": "activity",
            "activity": activity,
            "timestamp": int(time.time()),
        }
        await self._broadcast(out)

    # --------------------------------------------------------
    # GlobalSessionState 同步 (LLM 正在处理的会话 + 当前聊天目的)
    # --------------------------------------------------------

    def _on_global_state_change(self, change_type: str, **payload) -> None:
        """GlobalSessionState 变更回调 (同步入口, 异步广播)
        change_type: 'session' | 'purpose' | 'activity' | 'event_chain'
        """
        try:
            # 把最新的完整状态打包发给前端, 让前端自己 sync
            from brain.hc.global_session_state import get_global_session_state
            state = get_global_session_state()
            snapshot = {
                "sessions": state.get_sessions(),
                "event_chain_active": getattr(state, "_event_chain_active", False),
            }
            asyncio.create_task(self._broadcast({
                "type": "global_state_update",
                "state": snapshot,
                "change": change_type,
            }))
        except Exception as e:
            logger.warning(f"[Web:{self.name}] _on_global_state_change 失败: {e}")

    # --------------------------------------------------------
    # 身份管理 handlers
    # --------------------------------------------------------

    async def _handle_create_identity(self, data: Dict, ws: web.WebSocketResponse) -> None:
        sid = (data.get("session_id") or "").strip()
        uid = (data.get("user_id") or "").strip()
        uname = (data.get("user_name") or "").strip()
        switch_to = bool(data.get("switch_to", True))
        if not sid or not uid or not uname:
            await self._send_error(ws, "session_id / user_id / user_name 必填")
            return
        try:
            rt, warning = await self._identity_manager.create(sid, uid, uname, switch_to)
        except IdentityError as e:
            await self._send_error(ws, f"[{e.code}] {e.message}")
            return
        await self._broadcast_runtime(rt)
        if warning == "private_create":
            await self._send_warning(
                ws, "private_create",
                "你在私聊中创建了一个新身份。后续消息将以该身份发送, 注意对话连续性。",
                session_id=sid,
            )

    async def _handle_update_identity(self, data: Dict, ws: web.WebSocketResponse) -> None:
        sid = (data.get("session_id") or "").strip()
        old_uid = (data.get("old_user_id") or "").strip()
        old_uname = (data.get("old_user_name") or "").strip()
        new_uid = (data.get("new_user_id") or "").strip() or None
        new_uname = (data.get("new_user_name") or "").strip() or None
        if not sid or not old_uid or not old_uname:
            await self._send_error(ws, "session_id / old_user_id / old_user_name 必填")
            return
        if new_uid is None and new_uname is None:
            await self._send_error(ws, "new_user_id / new_user_name 至少给一个")
            return
        try:
            rt, _ = await self._identity_manager.update(
                sid, old_uid, old_uname, new_uid, new_uname,
            )
        except IdentityError as e:
            await self._send_error(ws, f"[{e.code}] {e.message}")
            return
        await self._broadcast_runtime(rt)

    async def _handle_delete_identity(self, data: Dict, ws: web.WebSocketResponse) -> None:
        sid = (data.get("session_id") or "").strip()
        uid = (data.get("user_id") or "").strip()
        uname = (data.get("user_name") or "").strip()
        if not sid or not uid or not uname:
            await self._send_error(ws, "session_id / user_id / user_name 必填")
            return
        try:
            rt, was_active = await self._identity_manager.delete(sid, uid, uname)
        except IdentityError as e:
            await self._send_error(ws, f"[{e.code}] {e.message}")
            return
        await self._broadcast_runtime(rt)
        if was_active:
            await self._send_warning(
                ws, "active_identity_deleted",
                f"被删除的身份 ({uid}, {uname}) 正是激活身份, 已自动切换到 {rt.active_identity.user_name}",
                session_id=sid,
            )

    async def _handle_switch_identity(self, data: Dict, ws: web.WebSocketResponse) -> None:
        sid = (data.get("session_id") or "").strip()
        uid = (data.get("user_id") or "").strip()
        uname = (data.get("user_name") or "").strip()
        if not sid or not uid or not uname:
            await self._send_error(ws, "session_id / user_id / user_name 必填")
            return
        try:
            rt, warning = await self._identity_manager.switch(sid, uid, uname)
        except IdentityError as e:
            await self._send_error(ws, f"[{e.code}] {e.message}")
            return
        await self._broadcast_runtime(rt)
        if warning == "private_switch":
            await self._send_warning(
                ws, "private_switch",
                "你在私聊中切换了身份。这可能导致对话上下文与历史消息不匹配, 建议仅用于测试。",
                session_id=sid,
            )

    # --------------------------------------------------------
    # Truck 控制 (运行时开关)
    # --------------------------------------------------------

    async def _handle_get_trucks(self, ws: web.WebSocketResponse) -> None:
        from adapt.web.truck_control import get_state_snapshot
        try:
            await ws.send_json({
                "type": "truck_state",
                "trucks": get_state_snapshot(),
            })
        except Exception as e:
            await self._send_error(ws, f"get_trucks 失败: {e}")

    async def _handle_set_truck(self, data: Dict, ws: web.WebSocketResponse) -> None:
        from adapt.web.truck_control import set_truck_enabled
        tid = (data.get("truck_id") or "").strip()
        if not tid:
            await self._send_error(ws, "truck_id 必填")
            return
        if "enabled" not in data:
            await self._send_error(ws, "enabled 字段必填")
            return
        result = set_truck_enabled(tid, bool(data.get("enabled")))
        if not result.get("ok"):
            await self._send_error(ws, result.get("error", "未知错误"))
            return
        # 广播给所有客户端 (包括触发者)
        await self._broadcast({
            "type": "truck_state_update",
            "trucks": result.get("all", []),
            "changed": result.get("snapshot"),
        })

    # --------------------------------------------------------
    # 测试模式：重定向存储单例数据库
    # --------------------------------------------------------

    async def _handle_switch_test_mode(self, data: Dict, ws: web.WebSocketResponse) -> None:
        from memory.message_repository import switch_message_store
        from brain.hc.database.core import switch_memory_db

        enable = bool(data.get("enable"))
        # 测试隔离库路径（tests/fixtures/scope）
        scope_root = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "scope"
        if enable:
            await switch_message_store(scope_root / "chat_history", db_name="test_chat_history.db")
            await switch_memory_db(scope_root / "memory.db")
            # 隔离 amy：收敛真实 monitor 并清空双单例，使下次访问指向测试 amy 目录
            await self._redirect_amy(scope_root / "amy")
        else:
            # 恢复真实库（不传参回到默认路径）
            await switch_message_store()
            await switch_memory_db()
            # 恢复真实 amy：收敛测试 monitor 并清空双单例，下次访问重建真实实例
            await self._redirect_amy(None)
        await ws.send_json({
            "type": "test_mode_switched",
            "enable": enable,
            "ok": True,
        })

    @staticmethod
    async def _redirect_amy(persistence_dir) -> None:
        """重指向 amy 持久化目录（走模块公有 reset 入口，不触碰内部全局）。

        persistence_dir: 传入隔离目录则测试 amy；None 则下次访问重建真实 amy。
        """
        from brain.amy.emotional_interface import reset_emotional_interface, get_emotional_interface

        await reset_emotional_interface()
        if persistence_dir is not None:
            # 重建指向测试目录的 amy（bootstrap 系统 act + monitor）
            await get_emotional_interface(Path(persistence_dir))

    async def _handle_list_presets(self, ws: web.WebSocketResponse) -> None:
        """分块预设清单：{ memory: [...], chat: [...], amy: [...] }。"""
        from tests.preset_env import list_presets

        await ws.send_json({"type": "presets", "blocks": list_presets()})

    async def _handle_apply_env(self, data: Dict, ws: web.WebSocketResponse) -> None:
        """分块环境准备：按所选块还原预设（仅还原，无进阶操作）。

        data: { memory?, chat?, amy? }，各块预设名（"默认"或情景名），缺省/空 = 不动该块。
        """
        from tests.preset_env import apply_env

        memory = (data.get("memory") or "").strip() or None
        chat = (data.get("chat") or "").strip() or None
        amy = (data.get("amy") or "").strip() or None
        await apply_env(memory=memory, chat=chat, amy=amy)
        await ws.send_json({
            "type": "env_applied",
            "memory": memory,
            "chat": chat,
            "amy": amy,
            "ok": True,
        })

    async def _handle_capture_mirror(self, data: Dict, ws: web.WebSocketResponse) -> None:
        """抓取指定分块的正式（真实）环境为镜像预设并命名。

        data: { block: memory|chat|amy, name }。
        """
        from tests.preset_env import capture_mirror

        block = (data.get("block") or "").strip()
        name = (data.get("name") or "").strip()
        if not block or not name:
            await self._send_error(ws, "block 与 name 必填")
            return
        await capture_mirror(block, name)
        await ws.send_json({"type": "mirror_captured", "block": block, "name": name, "ok": True})

    async def _handle_copy_state(self, data: Dict, ws: web.WebSocketResponse) -> None:
        """复制当前（测试）状态为命名预设情景文件。

        data: { block: memory|chat|amy, name }。
        """
        from tests.preset_env import copy_state

        block = (data.get("block") or "").strip()
        name = (data.get("name") or "").strip()
        if not block or not name:
            await self._send_error(ws, "block 与 name 必填")
            return
        await copy_state(block, name)
        await ws.send_json({"type": "state_copied", "block": block, "name": name, "ok": True})

    async def _handle_delete_preset(self, data: Dict, ws: web.WebSocketResponse) -> None:
        """删除指定分块的某个预设情景文件。

        data: { block: memory|chat|amy, name }。
        """
        from tests.preset_env import delete_preset

        block = (data.get("block") or "").strip()
        name = (data.get("name") or "").strip()
        if not block or not name:
            await self._send_error(ws, "block 与 name 必填")
            return
        await delete_preset(block, name)
        await ws.send_json({"type": "preset_deleted", "block": block, "name": name, "ok": True})

    # --------------------------------------------------------
    # 出站：LLM 回复
    # --------------------------------------------------------

    async def send(self, target: OutboundTarget, payload: OutboundPayload) -> str:
        seg_dicts = [_segment_to_dict(seg) for seg in payload.segments]

        # 实时广播也把图片 file 规范成 /media/ 或 /llm-save/ 完整 URL。
        # 否则前端 normalizeImgUrl 处理不了 LLM 写出的相对/绝对路径，图片会渲染成超链接；
        # 历史消息走 _file_msg_to_dict 已规范化，所以刷新后才正常——这就是"必须刷新"的根因。
        for sd in seg_dicts:
            if sd.get("type") == "image" and sd.get("file"):
                url = _resolve_image_url(self, sd["file"])
                sd["file"] = url or f"/media/{_normalize_filename(sd['file'])}?unresolved=1"

        # 内部存的 session_id 是 "private_xxx" / "group_xxx"；注册表 key 是 "xxx"
        web_sid = target.target_id
        prefix = f"{target.scope.value}_"
        if web_sid.startswith(prefix):
            web_sid = web_sid[len(prefix):]

        # 先固定 message_id：广播给前端时要带上（前端用它作为消息 id 以便删除），
        # 且落库也用同一个 id，保证前端 id 与文件里的 message_id 一致，删除才能命中。
        message_id = uuid.uuid4().hex

        out = {
            "type": "llm_message",
            "session_id": web_sid,
            "message_id": message_id,
            "target": {"scope": target.scope.value, "target_id": target.target_id},
            "segments": seg_dicts,
            "text": payload.raw_text or "".join(
                seg.data.get("text", "") for seg in payload.segments if seg.type == "text"
            ),
            "timestamp": int(time.time()),
        }

        async with self._clients_lock:
            clients = list(self._clients)
        if not clients:
            logger.warning(
                f"[Web] send 时没有连接的浏览器，消息将丢失: "
                f"session={web_sid} text={out['text'][:60]!r}"
            )
            return ""

        dead: List[web.WebSocketResponse] = []
        for ws in clients:
            try:
                await ws.send_json(out)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)
            logger.warning(f"[Web] 清掉 {len(dead)} 个死连接")

        return message_id

    # --------------------------------------------------------
    # 广播
    # --------------------------------------------------------

    def _make_frontend_notifier(self):
        """构造一个 async notifier: 收到 warning+ 日志后, 广播 {type:'warning'} 到所有 ws 客户端."""
        channel_ref = self

        async def _notifier(level: int, level_name: str, logger_name: str, message: str) -> None:
            # 只在 WebChannel 还活着的时候推送; 已 stop 的 channel 直接忽略
            if channel_ref._site is None:
                return
            payload = {
                "type": "warning",
                "level": level_name,
                "logger": logger_name,
                "message": message,
                "timestamp": int(time.time()),
            }
            try:
                await channel_ref._broadcast(payload)
            except Exception:
                logger.debug("[Web] 广播 warning 失败", exc_info=True)

        return _notifier

    async def _broadcast(self, obj: Dict[str, Any]) -> None:
        async with self._clients_lock:
            clients = list(self._clients)
        dead: List[web.WebSocketResponse] = []
        for ws in clients:
            try:
                await ws.send_json(obj)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._clients_lock:
                for ws in dead:
                    self._clients.discard(ws)

    async def broadcast_context_updated(self, kind: str, event: str, chain_id: Optional[str]) -> None:
        """广播 LLM 上下文落盘/归档事件（前端上下文查看器即时刷新用）。

        Args:
            kind: 前端 kind（chat | pfca | proactive | social_chat ...）
            event: 'append'（active 文件追加一条）| 'archive'（归档清空）
            chain_id: 归属任务链 ID（链外写入为 None）
        """
        if self._site is None:
            return
        await self._broadcast({
            "type": "context_updated",
            "kind": kind,
            "event": event,
            "chain_id": chain_id,
            "timestamp": int(time.time()),
        })
