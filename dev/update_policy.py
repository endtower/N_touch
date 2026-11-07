"""Shared package boundary: update program files, never runtime/user data."""
from __future__ import annotations

import re
from pathlib import PurePosixPath

COMPONENTS = ("core", "snowluma", "ingress", "manager", "launcher")
LOCALES = {"cs", "de", "es", "fr", "it", "ja", "ko", "pl", "pt-BR", "ru", "tr", "zh-Hans", "zh-Hant"}
CORE_SOURCE = "components/core/source/"
CORE_RUNTIME = "components/core/runtime/"


def core_source_file(name: str) -> bool:
    """可修改的程序源码；用户数据始终在独立 data_root。"""
    if not name.startswith(CORE_SOURCE):
        return False
    relative = name[len(CORE_SOURCE):]
    parts = relative.split("/")
    if any(p.startswith(".env") and p != ".env.example" for p in parts):
        return False
    if any(p in ("__pycache__", ".git", "node_modules", "logs", "data", "persistence", "history") for p in parts):
        return False
    if relative.startswith("web_frontend/dist/"):
        return True
    if relative.startswith(("web_frontend/src/", "web_frontend/public/")):
        return True
    if relative.startswith("web_frontend/") and relative.count("/") == 1:
        return relative.endswith((".json", ".ts", ".html"))
    return relative.endswith((".py", ".md")) or relative in ("requirements.txt", "requirements.lock", ".env.example")


def safe_relative(name: str) -> str:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise ValueError(f"Invalid package path: {name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.endswith((".", " ")) or any(ord(c) < 32 for c in p)
           or any(c in p for c in '<>"|?*')
           or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", p)
           for p in parts):
        raise ValueError(f"Invalid package path: {name!r}")
    return str(PurePosixPath(name))


def allowed_file(component: str, name: str) -> bool:
    name = safe_relative(name)
    parts = name.split("/")
    if component == "manager" and name.startswith("components/manager/"):
        return allowed_file("core", "components/core/" + name[len("components/manager/"):])
    if component == "core" and name.startswith("components/core/"):
        return (name == "components/core/release.json" or core_source_file(name)
                or (name.startswith(CORE_RUNTIME) and len(parts) > 3
                    and not any(p in ("__pycache__", ".git") for p in parts)))
    if component in ("core", "manager", "ingress"):
        return name == f"{component}.exe" or (len(parts) > 1 and parts[0] == f"{component}_internal")
    if component == "snowluma":
        if len(parts) > 1:
            return parts[0] in ("native", "client", "webui")
        return name in ("index.mjs", "launcher.bat", "node.exe", "package.json", "package-lock.json",
                        "release.json") \
            or name.endswith((".js", ".mjs", ".cjs", ".md")) or name in ("LICENSE", "LICENSE.txt")
    if component == "launcher":
        if len(parts) > 1:
            return parts[0] in LOCALES | {"runtimes"} or name == "updater/AbyssUpdater.exe"
        return name.endswith(".dll") or name in (
            "AbyssLauncher.exe", "AbyssLauncher.deps.json", "AbyssLauncher.runtimeconfig.json", "createdump.exe")
    return False


def version_key(version: str) -> tuple:
    """SemVer comparison (including numeric prerelease identifiers); build metadata is ignored."""
    match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", version)
    if not match:
        raise ValueError(f"Invalid version: {version!r}")
    pre = match[4]
    ids = tuple((0, int(x)) if x.isdigit() else (1, x) for x in pre.split(".")) if pre else ()
    return (int(match[1]), int(match[2]), int(match[3]), pre is None, ids)
