#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体下载器 - 把 OneBot 推送的图片/表情包从 QQ CDN 下载到本地

设计目标：
- 在 adapt 边界把"URL 引用"转成"本地路径引用"
- 区分"图片"（sub_type=0）和"表情包"（sub_type=1），分别存到不同子目录
- 落盘路径：<MEDIA_BASE_DIR>/{stickers|images}/<YYYY-MM-DD>/<file_id>.<ext>
- 同 file_id 多次接收时跳过下载（用本地锁防止并发）
- 失败不阻塞消息处理（保留原 URL，仅日志 warning）
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from ..protocol import NormalizedEvent, MessageSegment

logger = logging.getLogger(__name__)


@dataclass
class MediaConfig:
    """媒体下载配置（从 OneBotConfig 映射出来）"""
    enabled: bool
    base_dir: Path
    timeout: float
    max_size_mb: int
    retries: int


# 扩展名推断优先级
_EXT_FROM_FILE_FIELD = re.compile(r"\.(jpg|jpeg|png|gif|webp|bmp|svg)$", re.IGNORECASE)
_EXT_FROM_URL = re.compile(r"\.([a-z0-9]{2,5})(?:\?|$)", re.IGNORECASE)


def _guess_extension(file_field: Optional[str], url: Optional[str]) -> str:
    """推断文件扩展名（不含点）"""
    if file_field:
        m = _EXT_FROM_FILE_FIELD.search(file_field)
        if m:
            return m.group(1).lower()
    if url:
        # 去掉 query string
        path = urlparse(url).path
        m = _EXT_FROM_FILE_FIELD.search(path)
        if m:
            return m.group(1).lower()
        m = _EXT_FROM_URL.search(url)
        if m:
            return m.group(1).lower()
    return "jpg"  # 默认


class MediaDownloader:
    """图片/表情包下载器（一个 Channel 持有一个实例）"""

    def __init__(self, config: MediaConfig):
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None
        # 按 file_id 串行化（防止同一图片被并发下载）
        self._file_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _target_dir(self, sub_type: int) -> Path:
        """sub_type=1 → stickers, sub_type=0 → images"""
        sub = "stickers" if sub_type == 1 else "images"
        date = datetime.now().strftime("%Y-%m-%d")
        return self._config.base_dir / sub / date

    def _get_file_lock(self, key: str) -> asyncio.Lock:
        """懒创建 file_id → Lock 映射（线程/任务安全）"""
        # 第一次访问时双检：避免每次都 await 锁
        lock = self._file_locks.get(key)
        if lock is not None:
            return lock
        # 这里用 _locks_guard 防止多协程同时创建同一个 key 的锁
        return self._file_locks.setdefault(key, asyncio.Lock())

    async def enrich(self, ev: "NormalizedEvent") -> None:
        """遍历 ev.segments，下载 image 段，更新 data 字段

        失败不影响其它段，失败时保留原 URL。
        """
        if not self._config.enabled:
            return

        # 收集所有需要下载的段（避免在修改列表时迭代）
        to_process = []
        for i, seg in enumerate(ev.segments):
            if seg.type == "image":
                to_process.append((i, seg))

        if not to_process:
            return

        for i, seg in to_process:
            await self._process_one(seg)

    async def _process_one(self, seg: "MessageSegment") -> None:
        url = seg.data.get("url")
        file_field = seg.data.get("file", "")
        sub_type = int(seg.data.get("sub_type", 0) or 0)

        if not url:
            return  # 没有 URL 没法下载

        # 推断 file_id（用 file 字段的 hash 部分，去掉扩展名）
        file_id = file_field.split(".")[0] if file_field else f"unknown_{hash(url) & 0xFFFFFFFF:08x}"
        ext = _guess_extension(file_field, url)

        dest_dir = self._target_dir(sub_type)
        dest_path = dest_dir / f"{file_id}.{ext}"
        rel_path = dest_path.relative_to(self._config.base_dir.parent.parent)  # 相对项目根

        # 已存在 → 直接更新 local_path
        if dest_path.exists():
            seg.data["local_path"] = str(rel_path).replace("\\", "/")
            seg.data["local_downloaded_at"] = "exists"
            logger.debug(f"[Media] hit cache: {rel_path}")
            return

        # 并发保护：同一 file_id 串行下载
        lock = self._get_file_lock(file_id)
        async with lock:
            # 再次检查（其他协程可能已经下完了）
            if dest_path.exists():
                seg.data["local_path"] = str(rel_path).replace("\\", "/")
                seg.data["local_downloaded_at"] = "exists"
                return

            try:
                client = await self._get_client()
                last_err: Optional[Exception] = None
                for attempt in range(self._config.retries + 1):
                    try:
                        resp = await client.get(url)
                        if resp.status_code != 200:
                            raise RuntimeError(f"HTTP {resp.status_code}")
                        # 大小检查
                        content = resp.content
                        size_mb = len(content) / (1024 * 1024)
                        if size_mb > self._config.max_size_mb:
                            raise RuntimeError(
                                f"file too large: {size_mb:.1f}MB > {self._config.max_size_mb}MB"
                            )
                        # 原子写
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
                        tmp_path.write_bytes(content)
                        tmp_path.replace(dest_path)
                        seg.data["local_path"] = str(rel_path).replace("\\", "/")
                        seg.data["local_downloaded_at"] = datetime.now().isoformat()
                        seg.data["local_size_bytes"] = len(content)
                        logger.debug(
                            f"[Media] downloaded: {rel_path} "
                            f"({size_mb:.2f}MB, sub_type={sub_type})"
                        )
                        return
                    except Exception as e:
                        last_err = e
                        if attempt < self._config.retries:
                            await asyncio.sleep(0.5 * (attempt + 1))
                # 所有重试用尽
                logger.warning(
                    f"[Media] download failed (kept original url): "
                    f"file_id={file_id} url={url[:80]}... err={last_err}"
                )
            except Exception as e:
                logger.warning(
                    f"[Media] download error: file_id={file_id} err={e}"
                )
                # 保留原 url，不影响消息处理


def build_media_config_from_adapt(
    media_enabled: bool,
    media_base_dir: str,
    media_download_timeout: int,
    media_download_max_size_mb: int,
    media_download_retries: int,
) -> MediaConfig:
    """从 adapt_settings.OneBotConfig 字段构造 MediaConfig"""
    return MediaConfig(
        enabled=media_enabled,
        base_dir=Path(media_base_dir),
        timeout=float(media_download_timeout),
        max_size_mb=media_download_max_size_mb,
        retries=media_download_retries,
    )
