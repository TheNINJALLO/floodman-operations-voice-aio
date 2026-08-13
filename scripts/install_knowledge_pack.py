#!/usr/bin/env python3
"""Install a versioned Floodman website knowledge pack into persistent storage.

The container image contains reviewed defaults under /opt/floodman. Pterodactyl stores mutable
configuration under /home/container/data. This migration updates the managed company-information
sections and AVA tool overlay once per pack version while preserving operational settings and custom
knowledge documents. Existing files are backed up before replacement.
"""
from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


class InstallError(RuntimeError):
    pass


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise InstallError(f"YAML root must be a mapping: {path}")
    return value


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_yaml(path: Path, value: dict[str, Any]) -> None:
    text = yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=100)
    _atomic_text(path, text)


def _within(parent: Path, child: Path) -> bool:
    parent = parent.resolve()
    child = child.resolve(strict=False)
    return child == parent or parent in child.parents


def _backup(path: Path, backup_dir: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / path.name
    if path.is_dir() and not path.is_symlink():
        shutil.copytree(path, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(path, destination, follow_symlinks=False)


def _copy_managed(source: Path, destination: Path) -> None:
    if not source.is_dir():
        raise InstallError(f"Managed knowledge source is missing: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{destination.name}.staging.{os.getpid()}")
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(source, staging)
    for path in staging.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)
        elif path.is_dir():
            os.chmod(path, 0o700)
    if destination.exists():
        shutil.rmtree(destination)
    os.replace(staging, destination)


def install(args: argparse.Namespace) -> dict[str, Any]:
    data_dir = Path(args.data_dir).resolve()
    config_dir = Path(args.config_dir).resolve()
    knowledge_dir = Path(args.knowledge_dir).resolve(strict=False)
    if not _within(data_dir, config_dir):
        raise InstallError(f"CONFIG_DIR must be inside DATA_DIR: {config_dir}")
    if not _within(data_dir, knowledge_dir):
        raise InstallError(f"KNOWLEDGE_DIR must be inside DATA_DIR: {knowledge_dir}")

    marker_dir = data_dir / "migrations"
    marker = marker_dir / f"knowledge-pack-{args.pack_version}.done"
    if marker.exists() and not args.force:
        return {"ok": True, "changed": False, "marker": str(marker), "reason": "already_installed"}

    image_config = Path(args.image_config)
    image_ava = Path(args.image_ava)
    image_knowledge = Path(args.image_knowledge)
    if not image_config.is_file():
        raise InstallError(f"Built-in Floodman config is missing: {image_config}")
    if not image_ava.is_file():
        raise InstallError(f"Built-in AVA overlay is missing: {image_ava}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = data_dir / "backups" / f"knowledge-pack-{args.pack_version}-{timestamp}"
    persistent_config = config_dir / "floodman.yaml"
    persistent_ava = config_dir / "ava" / "ai-agent.local.yaml"
    managed_dir = knowledge_dir / "managed"
    custom_dir = knowledge_dir / "custom"

    for path in (persistent_config, persistent_ava, managed_dir):
        _backup(path, backup_dir)

    built_in_config = _load_yaml(image_config)
    current_config = _load_yaml(persistent_config)
    if not isinstance(built_in_config.get("business"), dict):
        raise InstallError("Built-in config is missing business information")
    if not isinstance(built_in_config.get("knowledge"), dict):
        raise InstallError("Built-in config is missing knowledge metadata")

    # Preserve the operator's telephony, compliance, scheduling, transfer, upload, and Roomflow
    # settings. Replace only the managed public-company and knowledge-pack sections.
    merged_config = dict(current_config or built_in_config)
    for key, value in built_in_config.items():
        if key not in merged_config:
            merged_config[key] = value
    merged_config["business"] = built_in_config["business"]
    merged_config["knowledge"] = built_in_config["knowledge"]
    _atomic_yaml(persistent_config, merged_config)

    # Preserve custom AVA keys but make the reviewed Floodman tools and safe templates authoritative.
    current_ava = _load_yaml(persistent_ava)
    built_in_ava = _load_yaml(image_ava)
    _atomic_yaml(persistent_ava, _deep_merge(current_ava, built_in_ava))

    _copy_managed(image_knowledge, managed_dir)
    custom_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(custom_dir, 0o700)
    custom_readme = custom_dir / "README.md"
    if not custom_readme.exists():
        _atomic_text(
            custom_readme,
            """---\ntitle: Custom Floodman Knowledge Instructions\ncategory: internal\napproved: false\ntags: [instructions]\nreviewed_at: 2026-08-12\nsummary: Operator instructions; not available to callers.\n---\n\nPlace operator-approved Markdown documents in this directory. Set `approved: true` only after reviewing every customer-facing claim. Custom documents survive managed website-pack updates.\n""",
        )

    marker_dir.mkdir(parents=True, exist_ok=True)
    _atomic_text(
        marker,
        f"installed_at={datetime.now(timezone.utc).isoformat()}\npack_version={args.pack_version}\nbackup={backup_dir}\n",
    )
    return {
        "ok": True,
        "changed": True,
        "marker": str(marker),
        "backup": str(backup_dir),
        "config": str(persistent_config),
        "ava_overlay": str(persistent_ava),
        "managed_knowledge": str(managed_dir),
        "custom_knowledge": str(custom_dir),
    }


def main() -> int:
    data_dir = Path(os.getenv("DATA_DIR", "/home/container/data"))
    config_dir = Path(os.getenv("CONFIG_DIR", data_dir / "config"))
    knowledge_dir = Path(os.getenv("KNOWLEDGE_DIR", data_dir / "knowledge"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-version", default=os.getenv("KNOWLEDGE_PACK_VERSION", "2026-08-12.1"))
    parser.add_argument("--data-dir", default=str(data_dir))
    parser.add_argument("--config-dir", default=str(config_dir))
    parser.add_argument("--knowledge-dir", default=str(knowledge_dir))
    parser.add_argument("--image-config", default="/opt/floodman/config/floodman.yaml")
    parser.add_argument("--image-ava", default="/opt/floodman/config/ava/ai-agent.local.yaml")
    parser.add_argument("--image-knowledge", default="/opt/floodman/knowledge")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = install(args)
    except (InstallError, OSError, yaml.YAMLError) as exc:
        print(f"knowledge pack installation failed: {exc}", file=os.sys.stderr)
        return 1
    print(yaml.safe_dump(result, sort_keys=False).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
