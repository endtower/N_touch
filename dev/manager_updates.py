"""Independent component feeds, staging, and installation coordination.

The external updater owns file replacement. The manager remains the sole process
controller; receipts and journals live outside each component's program files.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import secrets
import subprocess
import tarfile
import uuid
import time
import zipfile
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web

from paths import app_root, backup_root, cache_root, data_root, is_frozen, managed_install, resource_root
from update_policy import COMPONENTS, allowed_file, safe_relative, version_key
from core_update_review import release_details, installed_files, local_changes

SNOW_FEED = "https://api.github.com/repos/SnowLuma/SnowLuma/releases/latest"
SNOW_RELEASES = "https://api.github.com/repos/SnowLuma/SnowLuma/releases"
MAX_UNPACKED = 4 * 1024**3


def read_json(path: Path, default=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else default


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def validate_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")):
        raise ValueError("更新源需要 HTTPS（本机测试允许 HTTP）")
    if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("更新地址无效")
    return url


def unpack(archive: Path, output: Path, component: str, upstream=False, package_format="binary") -> dict[str, str]:
    """Never call extractall: reject traversal, links, collisions, and data files."""
    output.mkdir(parents=True, exist_ok=True)
    items = []
    if zipfile.is_zipfile(archive):
        source = zipfile.ZipFile(archive)
        for member in source.infolist():
            if member.is_dir():
                continue
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                source.close()
                raise ValueError("更新包不得包含符号链接")
            items.append((member.filename, member.file_size, member))
        opener = source.open
    else:
        source = tarfile.open(archive, "r:gz")
        for member in source.getmembers():
            if member.isdir():
                continue
            if not member.isfile():
                source.close()
                raise ValueError("更新包不得包含链接或特殊文件")
            items.append((member.name, member.size, member))
        opener = source.extractfile
    try:
        if not items or len(items) > 50000 or sum(x[1] for x in items) > MAX_UNPACKED:
            raise ValueError("更新包为空或超过解压限制")
        # Official SnowLuma bundles may have one wrapper directory.
        prefix = ""
        if upstream and not any(name == "index.mjs" for name, _, _ in items):
            candidates = [name[:-len("index.mjs")] for name, _, _ in items if name.endswith("/index.mjs")]
            if len(candidates) != 1:
                raise ValueError("SnowLuma 包缺少唯一 index.mjs")
            prefix = candidates[0]
        files = {}
        seen = set()
        for original, _, member in items:
            safe_relative(original)  # Validate even skipped entries.
            if prefix and not original.startswith(prefix):
                raise ValueError("SnowLuma 包包含未知目录")
            name = original[len(prefix):] if prefix else original
            if not allowed_file(component, name):
                # Upstream ships config templates; never install these over user config.
                if upstream and name.split("/")[0] in ("config", "data", "logs"):
                    continue
                raise ValueError(f"更新包包含非程序文件: {name}")
            if name.casefold() in seen:
                raise ValueError(f"更新包包含重复路径: {name}")
            seen.add(name.casefold())
            path = output / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with opener(member) as src, path.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        source_core = component == "core" and package_format == "source-v1"
        if component == "core" and any(name.startswith("components/core/") for name in files) != source_core:
            raise ValueError("Core 程序包格式与文件布局不一致")
        if source_core and any(not name.startswith("components/core/") for name in files):
            raise ValueError("Core 源码包不得混入旧版 EXE 文件")
        source_manager = component == "manager" and any(n.startswith("components/manager/") for n in files)
        required = ["components/manager/source/manager_service.py", "components/manager/source/paths.py",
                    "components/manager/runtime/python.exe", "components/manager/release.json"] if source_manager else \
            ["components/core/source/main.py", "components/core/source/ingress_service.py",
                    "components/core/source/paths.py", "components/core/source/config/settings.py",
                    "components/core/source/config/truck_settings.py", "components/core/source/config/role_settings.py",
                    "components/core/source/brain/base/persona/persona_registry.py",
                    "components/core/source/brain/hc/database/default_initializer.py",
                    "components/core/source/web_frontend/dist/index.html",
                    "components/core/runtime/python.exe", "components/core/release.json"] if source_core else \
            [f"{component}.exe"] if component in ("core", "manager", "ingress") else \
            (["index.mjs", "node.exe", "package.json"] if component == "snowluma" else ["AbyssLauncher.exe"])
        if not all(name in files for name in required):
            raise ValueError(f"更新包缺少入口文件: {required}")
        if not source_core and component in ("core", "manager", "ingress") and not any(name.startswith(f"{component}_internal/") for name in files):
            raise ValueError("更新包缺少运行依赖目录")
        if component == "snowluma":
            version = read_json(output / "package.json", {}).get("version", "")
            version_key(version)
        return files
    finally:
        source.close()


class Updates:
    def __init__(self, service, token: str, base_url: str):
        self.service, self.token, self.base_url = service, token, base_url
        self.root = data_root() / "manager" / "updates"
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = cache_root() / "updates"
        self.jobs = backup_root() / "updates"
        self.config_path = data_root() / "manager" / "update_sources.json"
        self.lock = asyncio.Lock()
        self.rows = {}
        self.confirmations = {}
        self.service.update_guard = self.blocked

    def payload_root(self, name):
        """Ready files from older managers still refer to their original staging path."""
        ready = read_json(self.root / name / "ready.json", {})
        return Path(ready["payload"]) if ready.get("payload") else self.root / name / "payload"

    @staticmethod
    def source_directory(component="core"):
        return app_root() / "components" / component / "source"

    def source_info(self, component="core"):
        actual = installed_files(app_root(), component)
        return {"source_directory": str(self.source_directory(component)),
                "available": self.source_directory(component).is_dir(),
                "backups_directory": str(backup_root() / f"{component}-source"),
                "local_changes": local_changes(read_json(self.receipt_path(component), {}), actual, app_root(), component)}

    async def backup_source(self, component="core"):
        """Save the user's running source tree without restoring an official baseline."""
        async with self.lock:
            if self.active():
                raise ValueError("已有组件正在安装，完成后再备份源码")
            if not self.source_directory(component).is_dir():
                raise ValueError("尚未安装源码 Core；仓库开发代码请使用版本控制保留")
            return await asyncio.to_thread(self._backup_source, component)

    def _backup_source(self, component="core"):
        source = self.source_directory(component)
        folder = backup_root() / f"{component}-source" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
        files = {}
        for parent, directories, filenames in os.walk(source, followlinks=False):
            directories[:] = [n for n in directories if n not in ("__pycache__", ".git", "node_modules")]
            for child in [Path(parent), *(Path(parent) / n for n in directories + filenames)]:
                if child.is_symlink() or child.is_junction():
                    raise ValueError("源码包含链接，请先处理后再备份")
            for name in filenames:
                path = Path(parent) / name
                files[path.relative_to(source).as_posix()] = self.hash_file(path)
        if not files:
            raise ValueError("源码目录为空，未创建备份")
        folder.mkdir(parents=True)
        # An incomplete folder remains distinguishable if the process exits mid-copy.
        write_json(folder / "snapshot.json", {"state": "copying", "source": str(source)})
        for relative, digest in files.items():
            path = source / relative
            target = folder / "source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            if self.hash_file(target) != digest or self.hash_file(path) != digest:
                raise ValueError("备份期间源码发生变化，请停止编辑后重试；已复制内容保留")
        # Detect additions/deletions during the snapshot, including non-package file types.
        current = {}
        for parent, directories, filenames in os.walk(source, followlinks=False):
            directories[:] = [n for n in directories if n not in ("__pycache__", ".git", "node_modules")]
            for child in [Path(parent), *(Path(parent) / n for n in directories)]:
                if child.is_symlink() or child.is_junction():
                    raise ValueError("备份期间源码目录变为链接，请停止编辑后重试")
            for name in filenames:
                path = Path(parent) / name
                current[path.relative_to(source).as_posix()] = self.hash_file(path)
        if current != files:
            raise ValueError("备份期间源码发生变化，请停止编辑后重试")
        receipt = read_json(self.receipt_path(component), {})
        write_json(folder / "installation-receipt.json", receipt)
        write_json(folder / "snapshot.json", {"state": "complete", "source": str(source), "created_at": time.time(),
                   "version": self.version(component), "files": files, "automatic_restore": False})
        return {"ok": True, "path": str(folder), "file_count": len(files), "message": "源码备份已保存，当前程序未改变"}

    def config(self):
        defaults = read_json(app_root() / "update_defaults.json", {"auto_check": True, "sources": {}})
        value = read_json(self.config_path, defaults)
        return {"auto_check": bool(value.get("auto_check", True)),
                "sources": {name: value.get("sources", {}).get(name, SNOW_FEED if name == "snowluma" else "") for name in COMPONENTS}}

    def receipt_path(self, component):
        return app_root() / ".versions" / f"{component}.json"

    def target_root(self, component):
        if component == "snowluma":
            from core.supervisor import snowluma_root
            return snowluma_root()
        return app_root()

    def version(self, component):
        if component == "snowluma":
            # SnowLuma 程序目录可能被多个 app_root（源码运行 / 冻结安装）共用，
            # 而 receipt 各自保存在 app_root 下，另一份可能落后于真实安装。
            # 以目标目录里的 package.json 为准，避免更新后界面仍显示旧版本。
            installed = read_json(self.target_root(component) / "package.json", {})
            if installed.get("version"):
                return installed["version"]
        receipt = read_json(self.receipt_path(component), {})
        if receipt.get("version"):
            return receipt["version"]
        if component == "manager" and managed_install():
            return read_json(app_root() / "components/manager/release.json", {}).get("version", "未知")
        source_distribution = read_json(app_root() / "update_defaults.json", {}).get("core_distribution") == "source-v1"
        if component in ("core", "ingress") and source_distribution:
            installed = read_json(app_root() / "components/core/release.json", {})
            return installed.get("version", "未安装")
        if component == "snowluma":
            return "未安装"
        baseline = read_json(app_root() / "component_versions.json") or read_json(resource_root() / "component_versions.json", {})
        return baseline.get(component, "未知")

    def active(self):
        return read_json(self.root / "active.json")

    def blocked(self, component):
        active = self.active()
        return bool(active and (active["component"] == "manager" or component == active["component"] or component in active.get("targets", []) or
                              (active["component"] == "ingress" and component in ("core", "snowluma"))))

    def status(self):
        active = self.active()
        rows = []
        for name in COMPONENTS:
            try:
                current = self.version(name)
                row = {"component": name, "current": current, **self.rows.get(name, {})}
                ready = read_json(self.root / name / "ready.json")
                if ready and row.get("state") not in ("checking", "downloading", "error"):
                    row.update(state="ready", latest=ready["version"], message="更新包已就绪，可以安装")
                row["installable"] = not (not managed_install() and name in ("core", "ingress", "manager") and
                                           (not is_frozen() or os.getenv("ABYSS_RUN_MODE") == "source"))
                row["staged_version"] = ready["version"] if ready else ""
                if name == "core":
                    row["manual_update"] = True
                    row["review_required"] = True
                    row["source_available"] = self.source_directory().is_dir()
                if name == "ingress" and ((app_root() / "components/core/release.json").is_file()
                        or read_json(app_root() / "update_defaults.json", {}).get("core_distribution") == "source-v1"):
                    row.update(installable=False, managed_by="core", message="随 Core 源码组件维护，请通过 Core 更新")
                row["versions"] = [{"version": version, "local": bool(release.get("archive") or release.get("backup"))}
                                   for version, release in self.versions(name).items()]
                result = read_json(self.root / name / "result.json")
                if result:
                    row["result"] = result
                if active and active["component"] == name:
                    row["state"] = "installing"
                    if not self.worker_alive(active):
                        row["state"] = "recovery_required"
                        row["message"] = "更新中断，请点击恢复；其他组件可继续使用"
                rows.append(row)
            except (ValueError, OSError) as error:
                rows.append({"component": name, "current": "未知", "state": "error", "message": str(error)})
        return {"components": rows, "config": self.config(), "active": active is not None}

    @staticmethod
    def worker_alive(active):
        pid = active.get("worker_pid")
        if not pid and active.get("phase") == "preparing":
            pid = active.get("owner_pid")
        if not pid:
            return False
        try:
            if os.name == "nt":
                import ctypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.restype = ctypes.c_void_p
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
                handle = kernel.OpenProcess(0x1000, False, pid)
                if not handle:
                    return False
                try:
                    code = ctypes.c_ulong()
                    return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259)
                finally:
                    kernel.CloseHandle(handle)
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    async def fetch_json(self, url):
        validate_url(url)
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Abyss-Component-Updater"}) as client:
            async with client.get(url) as response:
                response.raise_for_status()
                validate_url(str(response.url))
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024**2:
                        raise ValueError("版本信息过大")
                return json.loads(raw)

    def versions(self, name):
        """Published releases plus retained packages and verified-version backups."""
        source = self.config()["sources"][name]
        catalog = read_json(self.root / "catalog" / f"{name}.json", {})
        releases = {r["version"]: r for r in catalog.get("releases", [])} if catalog.get("source") == source else {}
        # A completed install records the previous version and its exact owned files.
        receipts = []
        for jobs in (self.root / "jobs", self.jobs, backup_root() / "legacy-updates/jobs"):
            receipts.extend(jobs.glob("*/previous-receipt.json"))
            receipts.extend(jobs.glob("*/*/previous-receipt.json"))
        for path in receipts:
            try:
                job = read_json(path.parent / "job.json", {})
                receipt = read_json(path, {})
                if (job.get("component") != name or receipt.get("target_root") != str(self.target_root(name).resolve())
                        or not receipt.get("files") or not (path.parent / "journal.json").is_file()):
                    continue
                version_key(receipt["version"])
                releases[receipt["version"]] = {"component": name, "version": receipt["version"], "backup": str(path.parent)}
            except (ValueError, OSError, KeyError, TypeError):
                continue
        packages = [p for root in (self.root, self.cache, backup_root() / "legacy-updates") for p in (root / "packages" / name).glob("*/release.json")]
        for path in packages:
            try:
                release = read_json(path)
                version_key(release["version"])
                archive = path.parent / "archive.zip"
                if release.get("component") == name and archive.is_file():
                    releases[release["version"]] = {**release, "archive": str(archive)}
            except (ValueError, OSError, KeyError, TypeError):
                continue
        return dict(sorted(releases.items(), key=lambda item: version_key(item[0]), reverse=True))

    def save_catalog(self, name, source, releases):
        prior = read_json(self.root / "catalog" / f"{name}.json", {})
        merged = {r["version"]: r for r in prior.get("releases", [])} if prior.get("source") == source else {}
        merged.update({r["version"]: r for r in releases})
        write_json(self.root / "catalog" / f"{name}.json", {"source": source, "releases": list(merged.values())})

    @staticmethod
    def snow_release(feed):
        tag = feed["tag_name"]
        version_key(tag)
        candidates = [a for a in feed.get("assets", []) if a["name"] in (
            f"SnowLuma-{tag}-win-x64.zip", f"SnowLuma-{tag}-win-x64.tar.gz")]
        if len(candidates) != 1 or not candidates[0].get("digest", "").startswith("sha256:"):
            raise ValueError("官方发布包缺少 Windows 完整版或 SHA256 校验值")
        asset = candidates[0]
        value = {"schema": 1, "component": "snowluma", "version": tag.removeprefix("v"),
                 "url": asset["browser_download_url"], "sha256": asset["digest"][7:],
                 "size": asset["size"], "notes": feed.get("body", "")}
        return Updates.validate_release(value, "snowluma") | {"upstream": True}

    def manifest_releases(self, feed, name, source):
        if feed.get("schema") != 1 or feed.get("component") != name:
            raise ValueError("更新清单组件或格式不匹配")
        entries = feed.get("releases", [feed])
        if not isinstance(entries, list) or not entries or len(entries) > 5000:
            raise ValueError("版本列表为空或格式无效")
        releases = []
        for entry in entries:
            release = self.validate_release({"schema": 1, "component": name, **entry} if "releases" in feed else entry, name)
            release["url"] = validate_url(urljoin(source, release["url"]))
            releases.append(release)
        if len({r["version"] for r in releases}) != len(releases):
            raise ValueError("版本列表包含重复版本")
        return releases

    async def check_one(self, name, history=False):
        if self.blocked(name):
            return
        source = self.config()["sources"][name]
        if not source:
            self.rows[name] = {"state": "unconfigured", "message": "尚未设置更新源，可导入本地更新包"}
            return
        self.rows[name] = {"state": "checking"}
        try:
            feed = await self.fetch_json(source)
            if name == "snowluma" and source == SNOW_FEED:
                release = self.snow_release(feed)
                releases = [release]
                if history:
                    for entry in await self.fetch_json(SNOW_RELEASES + "?per_page=100"):
                        if entry.get("draft"): continue
                        try: releases.append(self.snow_release(entry))
                        except (ValueError, KeyError, TypeError): continue
            else:
                releases = self.manifest_releases(feed, name, source)
                release = max(releases, key=lambda r: version_key(r["version"]))
                if history and feed.get("versions_url"):
                    history_url = validate_url(urljoin(source, feed["versions_url"]))
                    releases.extend(self.manifest_releases(await self.fetch_json(history_url), name, history_url))
            validate_url(release["url"])
            self.save_catalog(name, source, releases)
            current = self.version(name)
            newer = current in ("未知", "未安装") or version_key(release["version"]) > version_key(current)
            self.rows[name] = {"state": "available" if newer else "current", "latest": release["version"],
                               "message": "发现新版本" if newer else "已是最新版本", "release": release}
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError, TypeError) as error:
            self.rows[name] = {"state": "error", "message": f"检查失败: {error}"}

    @staticmethod
    def validate_release(value, name):
        if value.get("component") != name or value.get("schema") != 1:
            raise ValueError("更新清单组件或格式不匹配")
        version_key(value["version"])
        digest = value.get("sha256", "")
        if len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise ValueError("更新清单缺少 SHA256")
        if not isinstance(value.get("size"), int) or not 0 < value["size"] <= MAX_UNPACKED:
            raise ValueError("更新包大小无效")
        release = {k: value[k] for k in ("component", "version", "url", "sha256", "size")}
        return release | (release_details(value) if name == "core" else {"notes": value.get("notes", "")})

    async def check(self, name=None, history=False):
        async with self.lock:
            await asyncio.gather(*(self.check_one(n, history) for n in ([name] if name else COMPONENTS)))
        return self.status()

    async def download(self, name, version=None, confirmation_token=None):
        async with self.lock:
            if self.active():
                raise ValueError("已有组件正在安装，完成后再试")
            row = self.rows.get(name, {})
            release = self.versions(name).get(version) if version else row.get("release")
            if version:
                version_key(version)
                if not release and name == "snowluma" and self.config()["sources"][name] == SNOW_FEED:
                    release = self.snow_release(await self.fetch_json(SNOW_RELEASES + "/tags/v" + version))
                    self.save_catalog(name, SNOW_FEED, [release])
            if not release:
                raise ValueError("未找到所选版本的更新包，请刷新版本列表或导入本地包")
            if name == "core":
                self.consume_confirmation("download", release["version"], confirmation_token)
            row = {"release": release, "latest": release["version"]}
            if release.get("backup"):
                await asyncio.to_thread(self.stage_backup, name, release)
                self.rows[name] = {**row, "state": "ready", "message": "旧版本备份已校验，可以安装"}
                return self.status()
            if release.get("archive"):
                archive = Path(release["archive"])
                if archive.stat().st_size != release["size"] or await asyncio.to_thread(self.hash_file, archive) != release["sha256"].lower():
                    raise ValueError("已保存的更新包校验失败，请重新下载或导入")
                await asyncio.to_thread(self.stage, name, archive, release)
                self.rows[name] = {**row, "state": "ready", "message": "所选版本已就绪，可以安装"}
                return self.status()
            folder = self.cache / name
            folder.mkdir(parents=True, exist_ok=True)
            archive = folder / "download.partial"
            self.rows[name] = {**row, "state": "downloading", "progress": 0}
            try:
                digest, total = hashlib.sha256(), 0
                timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
                async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": "Abyss-Component-Updater"}) as client:
                    async with client.get(validate_url(release["url"])) as response:
                        response.raise_for_status()
                        validate_url(str(response.url))
                        with archive.open("wb") as stream:
                            async for chunk in response.content.iter_chunked(1024 * 1024):
                                total += len(chunk)
                                if total > release["size"]:
                                    raise ValueError("下载大小超过清单声明")
                                stream.write(chunk)
                                digest.update(chunk)
                                self.rows[name]["progress"] = round(total * 100 / release["size"])
                if total != release["size"] or digest.hexdigest().lower() != release["sha256"].lower():
                    raise ValueError("下载包大小或 SHA256 不匹配")
                await asyncio.to_thread(self.stage, name, archive, release)
                await asyncio.to_thread(self.retain_package, name, archive, release)
                self.rows[name] = {**row, "state": "ready", "message": "下载完成，可以安装"}
            except Exception as error:
                self.rows[name] = {**row, "state": "available", "message": f"下载失败: {error}"}
                raise
            finally:
                archive.unlink(missing_ok=True)
        return self.status()

    def retain_package(self, name, archive, release):
        folder = self.cache / "packages" / name / release["version"]
        folder.mkdir(parents=True, exist_ok=True)
        temp = folder / "archive.tmp"
        shutil.copy2(archive, temp)
        temp.replace(folder / "archive.zip")
        write_json(folder / "release.json", {k: v for k, v in release.items() if k not in ("archive", "backup")})

    def stage_backup(self, name, release):
        folder = Path(release["backup"])
        receipt = read_json(folder / "previous-receipt.json")
        archive = self.cache / name / "backup.partial"
        archive.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
                for entry, digest in receipt["files"].items():
                    if not allowed_file(name, entry): raise ValueError("旧版本备份包含非程序文件")
                    path = folder / "backup" / entry
                    if path.is_symlink() or path.is_junction() or self.hash_file(path) != digest:
                        raise ValueError("旧版本备份不完整或校验失败")
                    output.write(path, entry)
            saved = {"component": name, "version": release["version"], "url": "",
                     "sha256": self.hash_file(archive), "size": archive.stat().st_size, "notes": "本机更新前的程序备份"}
            if name == "core":
                saved.update(receipt.get("release") or {})
            self.stage(name, archive, saved)
            self.retain_package(name, archive, saved)
        finally:
            archive.unlink(missing_ok=True)

    def stage(self, name, archive, release):
        folder = self.root / name
        payload = self.cache / name / "payload"
        if payload.exists():
            shutil.rmtree(payload)
        (folder / "ready.json").unlink(missing_ok=True)
        details = release_details(release) if name == "core" else {}
        files = unpack(archive, payload, name, release.get("upstream", False), details.get("format", "binary"))
        if name == "core" and details["format"] == "source-v1":
            internal = read_json(payload / "components/core/release.json", {})
            if (internal.get("version") != release["version"] or internal.get("format") != "source-v1"
                    or release_details(internal) != details):
                raise ValueError("Core 源码包内部版本与发布清单不一致")
        if name == "snowluma" and read_json(payload / "package.json")["version"] != release["version"]:
            raise ValueError("SnowLuma 包内版本不匹配")
        if name == "manager" and (payload / "components/manager/release.json").is_file():
            if read_json(payload / "components/manager/release.json").get("version") != release["version"]:
                raise ValueError("管理服务源码包内部版本与发布清单不一致")
        write_json(folder / "ready.json", {"component": name, "version": release["version"], "files": files,
                                         "release": details, "payload": str(payload.resolve())})

    async def import_local(self, name, path):
        async with self.lock:
            if self.active():
                raise ValueError("已有组件正在安装")
            archive = Path(path).resolve()
            manifest = archive.with_suffix(".json")
            release = self.validate_release(read_json(manifest, {}), name)
            if archive.stat().st_size != release["size"] or await asyncio.to_thread(self.hash_file, archive) != release["sha256"].lower():
                raise ValueError("本地包校验失败；需要同名 .json 清单")
            await asyncio.to_thread(self.stage, name, archive, release)
            await asyncio.to_thread(self.retain_package, name, archive, release)
            self.rows[name] = {"state": "ready", "latest": release["version"], "message": "本地包已校验，可以安装"}
        return self.status()

    @staticmethod
    def hash_file(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    def core_review_state(self, operation, version=None):
        if operation not in ("download", "install", "recover"):
            raise ValueError("未知的 Core 确认操作")
        ready = read_json(self.root / "core/ready.json")
        if operation == "recover":
            active = self.active()
            if not active or active["component"] != "core":
                raise ValueError("没有需要人工确认数据的 Core 恢复任务")
            result = read_json(self.root / "core/result.json", {})
            if not result.get("data_restore_required"):
                raise ValueError("该恢复任务无需人工确认数据")
            job = read_json(Path(active["job"]))
            release = {"version": self.version("core"), **job.get("release", {})}
            ready = {"active": active, "result": result}
        elif operation == "install":
            if not ready:
                raise ValueError("请先下载或导入 Core 更新包")
            if version and ready["version"] != version:
                raise ValueError("已准备的更新包与所选版本不同")
            release = {"version": ready["version"], **ready.get("release", {})}
        else:
            release = self.versions("core").get(version) if version else self.rows.get("core", {}).get("release")
            if not release:
                raise ValueError("未找到所选 Core 版本，请先刷新版本列表")
        details = release_details(release)
        actual = installed_files(app_root())
        receipt = read_json(self.receipt_path("core"), {})
        changed = local_changes(receipt, actual, app_root())
        blockers = []
        if operation == "install":
            if not managed_install() and (not is_frozen() or os.getenv("ABYSS_RUN_MODE") == "source"):
                blockers.append("当前运行仓库源码，保留现有代码；请在独立安装目录使用组件更新")
            if changed:
                blockers.append("检测到本地程序修改，默认保留；请先在副本中合并或备份处理后再更新")
            for component, minimum in details["requires"].items():
                current = self.version(component)
                if current in ("未知", "未安装") or version_key(current) < version_key(minimum):
                    blockers.append(f"需要 {component} >= {minimum}，当前为 {current}")
        shared = details["format"] == "source-v1" or (app_root() / "components/core/release.json").is_file()
        affected = ["ingress", "core"] if shared or details["data_policy"] == "migration" else ["core"]
        state = {"operation": operation, "version": release["version"], "current": self.version("core"),
                 "release": details, "local_changes": changed, "affected": affected,
                 "blockers": blockers, "actual_files": actual,
                 "ready": ready if operation in ("install", "recover") else release}
        return state

    @staticmethod
    def review_digest(state):
        return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    async def review_core(self, operation, version=None):
        async with self.lock:
            if self.active() and operation != "recover":
                raise ValueError("已有组件正在安装")
            state = await asyncio.to_thread(self.core_review_state, operation, version)
            now = time.monotonic()
            self.confirmations = {k: v for k, v in self.confirmations.items() if v[0] > now}
            token = secrets.token_urlsafe(32) if not state["blockers"] else ""
            if token:
                self.confirmations[token] = (now + 600, operation, state["version"], self.review_digest(state))
            return {k: state[k] for k in ("operation", "version", "current", "release", "local_changes", "affected", "blockers")} | {
                "confirmation_token": token, "can_proceed": not state["blockers"],
                "backup_note": "安装前备份被替换的程序、Core 配置与状态（消息接入同时停止时包含消息库）；不复制媒体附件或第三方账号数据。新任务保存在 backups/updates，旧任务保持原位。程序回退不会自动覆盖用户数据；修改过的源码可先单独备份。"}

    def consume_confirmation(self, operation, version, token):
        saved = self.confirmations.pop(token, None)
        if not saved or saved[0] <= time.monotonic() or saved[1:3] != (operation, version):
            raise ValueError("Core 需要先查看说明并确认本次操作；确认已过期或与版本不匹配")
        state = self.core_review_state(operation, version)
        if state["blockers"] or self.review_digest(state) != saved[3]:
            raise ValueError("Core 版本、发布说明或本地文件已变化，请重新检查并确认")
        return state

    def backup_core_data(self, folder, include_messages=False):
        """调用方已停止所有相应写入进程；只复制明确的数据文件，不打开活动数据库。"""
        root = data_root()
        sources = [root / ".env.dev", root / "manager/trucks.json", root / "memory/memory.db"]
        sources += list((root / "memory").glob("memory.db-*"))
        for relative in ("user_settings", "brain/base/persona", "brain/amy/persistence", "adapt/web/data", "brain/hc/shortlight/notes"):
            directory = root / relative
            if directory.is_dir():
                sources += list(directory.rglob("*.json"))
        # Message, inbox and bridge databases are nested under memory/chat_history.
        # Preserve JSON histories too, but do not duplicate large attachment stores.
        for parent, directories, filenames in os.walk(root / "memory", followlinks=False):
            directories[:] = [n for n in directories if n not in ("media", "llm_save", "backups", "__pycache__")]
            for name in directories:
                child = Path(parent) / name
                if child.is_symlink() or child.is_junction():
                    raise ValueError("数据备份包含链接，拒绝继续安装")
            for name in filenames:
                if name.endswith(".json") or (include_messages and name.endswith((".db", ".db-wal", ".db-shm"))):
                    sources.append(Path(parent) / name)
        files = {}
        for source in set(sources):
            if source.is_file():
                if any(p.is_symlink() or p.is_junction() for p in (source, *source.parents) if p != root.parent):
                    raise ValueError("数据备份包含链接，拒绝继续安装")
                target = folder / "data-backup" / source.relative_to(root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                files[source.relative_to(root).as_posix()] = self.hash_file(target)
        write_json(folder / "data-backup/backup.json", {"created_at": time.time(), "source": str(root),
                   "automatic_restore": False, "include_messages": include_messages, "files": files,
                   "scope": "core-config-state-and-stopped-databases", "includes_media": False,
                   "includes_third_party_data": False})

    def validate_core_source(self, folder):
        payload = self.payload_root("core") / "components/core"
        if not (payload / "release.json").is_file():
            return
        source = payload / "source"
        env = os.environ.copy()
        from config_keys import CONFIG_OVERRIDE_KEYS
        for key in CONFIG_OVERRIDE_KEYS:
            env.pop(key, None)
        preflight = self.cache / "preflight"
        preflight.mkdir(parents=True, exist_ok=True)
        code = ("import sys;sys.path.insert(0,sys.argv[1]);"
                "import config,adapt.ingress_runtime,brain.pfc.core;"
                "from brain.att.proactive.proactive_session import ProactiveSession;"
                "assert not ProactiveSession.__abstractmethods__;"
                "ProactiveSession({'intent':'pre_opportunity'});print('CORE_PREFLIGHT_OK')")
        with tempfile.TemporaryDirectory(prefix="core-", dir=preflight) as temporary:
            env["ABYSS_DATA_DIR"] = temporary
            result = subprocess.run([str(payload / "runtime/python.exe"), "-I", "-B", "-c", code, str(source)],
                                    cwd=temporary, env=env, capture_output=True, timeout=90,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or b"CORE_PREFLIGHT_OK" not in result.stdout:
            raise ValueError("Core 源码组件启动检查失败：" + (result.stderr or result.stdout)[-4000:].decode("utf-8", errors="replace"))

    async def apply(self, name, launcher_pid=0, version=None, confirmation_token=None):
        async with self.lock:
            if self.active():
                raise ValueError("已有组件正在安装")
            if not managed_install() and name in ("core", "ingress", "manager") and (not is_frozen() or os.getenv("ABYSS_RUN_MODE") == "source"):
                raise ValueError("当前为源码运行模式；请从正式发布版使用二进制更新")
            ready = read_json(self.root / name / "ready.json")
            if not ready:
                raise ValueError("请先下载或导入更新包")
            if version is not None and ready["version"] != version:
                raise ValueError("已准备的更新包与所选版本不同，请先准备所选版本")
            if managed_install() and name in ("core", "manager") and not any(p.startswith(f"components/{name}/source/") for p in ready["files"]):
                raise ValueError("源码式安装不能被旧版二进制组件覆盖，请使用源码发行包")
            if any(not (self.payload_root(name) / relative).is_file() for relative in ready["files"]):
                raise ValueError("准备的更新文件已丢失，请重新下载或导入；当前程序未停止")
            review = self.consume_confirmation("install", ready["version"], confirmation_token) if name == "core" else None
            if name == "ingress" and ((app_root() / "components/core/release.json").is_file()
                    or read_json(app_root() / "update_defaults.json", {}).get("core_distribution") == "source-v1"):
                raise ValueError("消息接入属于 Core 源码组件，请通过 Core 更新")
            single_launcher = managed_install()
            updater = app_root() / ("AbyssLauncher.exe" if single_launcher else "updater/AbyssUpdater.exe")
            if not updater.is_file():
                raise ValueError("发布目录缺少独立更新器，请安装完整发布文件")
            if name == "launcher" and launcher_pid <= 0:
                raise ValueError("启动器更新必须从桌面更新中心发起")
            job_id = uuid.uuid4().hex
            folder = self.jobs / job_id
            folder.mkdir(parents=True)
            # Self-contained helper copied out of the files it may replace.
            helper = folder / ("AbyssLauncher.exe" if single_launcher else "AbyssUpdater.exe")
            shutil.copy2(updater, helper)
            targets = list(self.service.controllers) if name == "manager" else \
                (review["affected"] if review else [name] if name in self.service.controllers else [])
            target = self.target_root(name).resolve()
            old = read_json(self.receipt_path(name), {})
            manager_files = installed_files(app_root(), "manager") if name == "manager" and single_launcher else None
            if manager_files is not None and local_changes(old, manager_files, app_root(), "manager"):
                raise ValueError("管理服务源码存在本地修改，请先备份并在副本中合并，当前源码不会被覆盖")
            owned = list(old["files"]) if old.get("files") and old.get("target_root") == str(target) else []
            if name == "snowluma" and owned:
                # receipt 可能来自另一个 app_root 且落后于真实安装；按旧 receipt 计算
                # 待删除文件会漏掉当前版本的文件。版本不一致时改为扫描目标目录。
                installed = read_json(target / "package.json", {}).get("version")
                if installed != old.get("version"):
                    owned = []
            if not owned:
                owned = [p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file() and
                         allowed_file(name, p.relative_to(target).as_posix())]
            # Block new control commands before stopping, including the console routes.
            with self.service.epoch_lock:
                previous = [n for n in targets if self.service.controller(n).snapshot().desired == "running"]
                active = {"id": job_id, "component": name, "job": str(folder / "job.json"), "worker_pid": 0,
                          "phase": "preparing", "owner_pid": os.getpid(), "previous": previous, "targets": targets}
                job = {**ready, "id": job_id, "target_root": str(target), "app_root": str(app_root()),
                       "data_root": str(data_root()), "payload": str(self.payload_root(name)),
                       "previous_files": owned, "receipt": str(self.receipt_path(name)), "manager_url": self.base_url,
                       "result": str(self.root / name / "result.json"), "previous": previous,
                       "wait_pid": os.getpid() if name == "manager" else (launcher_pid if name == "launcher" else 0)}
                if review:
                    job["expected_files"] = review["actual_files"]
                    job["protect_local_changes"] = True
                    job["release"] = review["release"]
                if manager_files is not None:
                    job["expected_files"] = manager_files
                    job["protect_local_changes"] = True
                # Persist a recoverable job BEFORE the first stop, even if the manager crashes here.
                write_json(folder / "job.json", job)
                write_json(self.root / "active.json", active)
            try:
                if name == "core":
                    await asyncio.to_thread(self.validate_core_source, folder)
                for n in sorted(targets, key=lambda n: self.service.controller(n).spec.order, reverse=True):
                    snapshot = self.service.controller(n).snapshot()
                    if name == "manager" and not getattr(snapshot, "pid", None) and snapshot.desired != "running":
                        continue  # An unrelated external port does not belong to this manager.
                    result = await asyncio.to_thread(self.service.controller(n).submit, "stop", True)
                    if not result.get("ok"):
                        raise ValueError(f"无法停止 {n}: {result.get('message')}")
                    if result.get("error_code") == "external_port":
                        raise ValueError(f"{n} 端口由外部实例占用，无法确认文件已释放")
                if name == "core":
                    await asyncio.to_thread(self.backup_core_data, folder, "ingress" in targets)
                (self.root / name / "result.json").unlink(missing_ok=True)
                proc = subprocess.Popen(self.worker_command(folder / "job.json"), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                active["worker_pid"] = proc.pid
                active["phase"] = "installing"
                write_json(self.root / "active.json", active)
                if name == "manager":
                    asyncio.get_running_loop().call_later(1.0, lambda: os._exit(0))
                return {"ok": True, "exit_launcher": name == "launcher", "job": job_id}
            except Exception:
                (self.root / "active.json").unlink(missing_ok=True)
                for n in previous:
                    await asyncio.to_thread(self.service.controller(n).submit, "start")
                raise

    async def finish(self, job_id):
        async with self.lock:
            active = self.active()
            if not active or active["id"] != job_id:
                raise ValueError("更新任务不匹配")
            result = read_json(self.root / active["component"] / "result.json", {})
            if result.get("id") != job_id or result.get("state") not in ("installed", "rolled_back", "cancelled"):
                raise ValueError("更新尚未完成；若中断请恢复")
            restart_errors = []
            for name in sorted(active["previous"], key=lambda n: self.service.controller(n).spec.order):
                started = await asyncio.to_thread(self.service.controller(name).submit, "start", True)
                if not started.get("ok"):
                    restart_errors.append(f"{name}: {started.get('message')}")
            if restart_errors:
                result["message"] += "；程序已安装，但启动失败: " + "; ".join(restart_errors)
                if active["component"] == "core" and result["state"] == "installed":
                    job_path = Path(active["job"])
                    job = read_json(job_path)
                    for name in sorted(job.get("previous", []), key=lambda n: self.service.controller(n).spec.order, reverse=True):
                        stopped = await asyncio.to_thread(self.service.controller(name).submit, "stop", True)
                        if not stopped.get("ok"):
                            raise ValueError("新版启动失败且无法停止，请保留安装现场后恢复")
                    rollback = await asyncio.to_thread(subprocess.run,
                        self.worker_command(job_path, "--rollback", "--files-only"),
                        capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if rollback.returncode:
                        result.update(state="recovery_required", message="新版启动失败，程序恢复未完成，请保留更新任务目录")
                    elif job.get("release", {}).get("data_policy") != "compatible":
                        result.update(state="recovery_required", data_restore_required=True,
                                      message="新版启动失败，已恢复旧程序并停止相关服务；请按迁移说明确认数据备份与恢复，再继续运行")
                    else:
                        result.update(state="rolled_back", message="新版启动检查失败，已恢复上一版程序")
                        for name in sorted(active["previous"], key=lambda n: self.service.controller(n).spec.order):
                            restored = await asyncio.to_thread(self.service.controller(name).submit, "start", True)
                            if not restored.get("ok"):
                                result.update(state="recovery_required", message="已恢复旧程序，但服务未能启动，请检查日志")
                write_json(self.root / active["component"] / "result.json", result)
            if result.get("state") == "recovery_required":
                active["worker_pid"] = 0
                write_json(self.root / "active.json", active)
                return {"ok": False, **result}
            (self.root / "active.json").unlink(missing_ok=True)
            self.rows.pop(active["component"], None)
            (self.root / active["component"] / "ready.json").unlink(missing_ok=True)
            return {"ok": True, **result}

    async def recover(self, launcher_pid=0, confirmation_token=None):
        async with self.lock:
            active = self.active()
            if not active or self.worker_alive(active):
                raise ValueError("没有中断的更新任务")
            path = Path(active["job"])
            job = read_json(path)
            result = read_json(self.root / active["component"] / "result.json", {})
            if result.get("data_restore_required"):
                self.consume_confirmation("recover", self.version("core"), confirmation_token)
                for name in sorted(active["previous"], key=lambda n: self.service.controller(n).spec.order):
                    started = await asyncio.to_thread(self.service.controller(name).submit, "start", True)
                    if not started.get("ok"):
                        raise ValueError("人工确认后旧版仍未启动，请检查日志：" + str(started.get("message")))
                result.update(state="rolled_back", data_restore_required=False, message="用户已确认数据处理，旧版程序已恢复运行；数据未被自动覆盖")
                write_json(self.root / "core/result.json", result)
                (self.root / "active.json").unlink()
                (self.root / "core/ready.json").unlink(missing_ok=True)
                self.rows.pop("core", None)
                return {"ok": True, **result}
            if active["component"] == "launcher" and launcher_pid <= 0:
                raise ValueError("启动器恢复必须从桌面更新中心发起")
            job["wait_pid"] = os.getpid() if active["component"] == "manager" else (launcher_pid if active["component"] == "launcher" else 0)
            write_json(path, job)
            proc = subprocess.Popen(self.worker_command(path, "--recover"),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            active["worker_pid"] = proc.pid
            write_json(self.root / "active.json", active)
            if active["component"] == "manager":
                asyncio.get_running_loop().call_later(1.0, lambda: os._exit(0))
            return {"ok": True, "exit_launcher": active["component"] == "launcher"}

    @staticmethod
    def worker_command(path, *options):
        launcher = path.parent / "AbyssLauncher.exe"
        return [str(launcher), "--update-job", str(path), *options] if launcher.is_file() else \
            [str(path.parent / "AbyssUpdater.exe"), str(path), *options]


def register_updates(app, updates: Updates, authorized):
    async def dispatch(request):
        if not authorized(request):
            return web.json_response({"message": "管理令牌无效"}, status=401)
        try:
            action = request.match_info.get("action", "status")
            body = await request.json() if request.can_read_body else {}
            name = body.get("component")
            if name is not None and name not in COMPONENTS:
                raise ValueError("未知组件")
            if action == "status":
                result = updates.status()
            elif action == "config":
                async with updates.lock:
                    sources = body.get("sources", {})
                    if not isinstance(sources, dict) or set(sources) - set(COMPONENTS):
                        raise ValueError("更新源格式错误")
                    for url in sources.values():
                        if url:
                            validate_url(url)
                    write_json(updates.config_path, {"auto_check": bool(body.get("auto_check", True)), "sources": sources})
                    updates.rows.clear()
                result = updates.status()
            elif action == "check":
                result = await updates.check(name, bool(body.get("history", False)))
            elif action == "recover":
                result = await updates.recover(int(body.get("launcher_pid", 0)), body.get("confirmation_token"))
            elif action == "finish":
                result = await updates.finish(body["id"])
            elif name is None:
                raise ValueError("请指定组件")
            elif action == "download":
                result = await updates.download(name, body.get("version"), body.get("confirmation_token"))
            elif action == "import":
                result = await updates.import_local(name, body["path"])
            elif action == "apply":
                result = await updates.apply(name, int(body.get("launcher_pid", 0)), body.get("version"), body.get("confirmation_token"))
            elif action == "review":
                if name != "core":
                    raise ValueError("此确认流程只用于 Core")
                result = await updates.review_core(body.get("operation"), body.get("version"))
            elif action in ("source", "source-backup"):
                if name not in ("core", "manager"):
                    raise ValueError("此操作只用于 Core 或管理服务源码")
                result = await updates.backup_source(name) if action == "source-backup" else await asyncio.to_thread(updates.source_info, name)
            else:
                raise ValueError("未知更新操作")
            return web.json_response(result)
        except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile, tarfile.TarError,
                aiohttp.ClientError, asyncio.TimeoutError) as error:
            return web.json_response({"ok": False, "message": str(error)}, status=400)

    app.router.add_get("/v1/updates", dispatch)
    app.router.add_post("/v1/updates/{action:check|download|import|apply|review|config|finish|recover|source|source-backup}", dispatch)
