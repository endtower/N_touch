#!/usr/bin/env python3
"""Create one program-only ZIP and its independent update feed (no private data)."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from update_policy import COMPONENTS, allowed_file, version_key
from core_update_review import release_details
from workspace_layout import BUILDS


def package(component: str, version: str, source: Path, output: Path, base_url="", notes="", metadata=None) -> Path:
    version_key(version)
    if component not in COMPONENTS:
        raise ValueError("Unknown component")
    source = source.resolve()
    output = output.resolve()
    if output == source or output.is_relative_to(source):
        raise ValueError("Output must be outside the input directory")
    files = sorted(p for p in source.rglob("*") if p.is_file() and
                   allowed_file(component, p.relative_to(source).as_posix()))
    details = release_details(metadata or {}) if component == "core" else {}
    internal = source / "components/core/release.json"
    if component == "core" and internal.is_file():
        internal_data = json.loads(internal.read_text(encoding="utf-8"))
        details = release_details(internal_data | (metadata or {}))
        if internal_data.get("version") != version or details["format"] != "source-v1":
            raise ValueError("Core source package metadata does not match its release")
    required = "components/manager/source/manager_service.py" if component == "manager" and (source / "components/manager/release.json").is_file() else \
        "components/core/source/main.py" if details.get("format") == "source-v1" else \
        f"{component}.exe" if component in ("core", "manager", "ingress") else \
        ("index.mjs" if component == "snowluma" else "AbyssLauncher.exe")
    if not (source / required).is_file() or not files:
        raise ValueError(f"Missing entry point: {source / required}")
    if component == "snowluma" and json.loads((source / "package.json").read_text(encoding="utf-8"))["version"] != version:
        raise ValueError("SnowLuma version must match package.json")
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"{component}-{version}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            if path.is_symlink() or path.is_junction():
                raise ValueError(f"Refusing linked program file: {path}")
            archive.write(path, path.relative_to(source).as_posix())
    with target.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {"schema": 1, "component": component, "version": version,
                "url": base_url.rstrip("/") + "/" + target.name if base_url else target.name,
                "sha256": digest, "size": target.stat().st_size, "notes": notes}
    if component == "core":
        manifest.update(details)
        if notes:
            manifest["notes"] = notes
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    target.with_suffix(".json").write_text(text, encoding="utf-8")
    releases = {}
    for path in output.glob(f"{component}-*.json"):
        if path.name in (f"{component}-latest.json", f"{component}-versions.json"): continue
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("component") == component and value.get("schema") == 1 and value.get("version"):
            version_key(value["version"])
            releases[value["version"]] = value
    catalog = {"schema": 1, "component": component,
               "releases": sorted(releases.values(), key=lambda r: version_key(r["version"]), reverse=True)}
    (output / f"{component}-versions.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest = {**manifest, "versions_url": f"{component}-versions.json"}
    (output / f"{component}-latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=COMPONENTS)
    parser.add_argument("--version", required=True)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=BUILDS / "component-releases")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--notes", default="")
    parser.add_argument("--metadata", type=Path, help="Core 发布说明、破坏性变更、兼容范围及数据迁移说明 JSON")
    args = parser.parse_args()
    print(package(args.component, args.version, args.input, args.output, args.base_url, args.notes,
                  json.loads(args.metadata.read_text(encoding="utf-8")) if args.metadata else None))


if __name__ == "__main__":
    main()
