#!/usr/bin/env python3
"""Create a consistent, verifiable Floodman Voice backup while services are live."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_backup(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"file:{source.resolve()}?mode=ro"
    source_conn = sqlite3.connect(source_uri, uri=True, timeout=15)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.execute("PRAGMA busy_timeout=15000")
        source_conn.backup(destination_conn, pages=256, sleep=0.05)
        result = destination_conn.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise RuntimeError(f"SQLite integrity check failed for {source}")
    finally:
        destination_conn.close()
        source_conn.close()
    destination.chmod(0o600)
    return True


def copy_file(source: Path, destination: Path, mode: int = 0o600) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    destination.chmod(mode)
    return True


def copy_tree(source: Path, destination: Path) -> bool:
    if not source.is_dir():
        return False
    shutil.copytree(source, destination, symlinks=True)
    for path in destination.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o600)
        elif path.is_dir() and not path.is_symlink():
            path.chmod(0o700)
    return True


def verify_staging(staging_root: Path, manifest: dict[str, Any]) -> None:
    for entry in manifest["files"]:
        path = staging_root / entry["path"]
        if not path.is_file():
            raise RuntimeError(f"Backup file missing during verification: {entry['path']}")
        if sha256(path) != entry["sha256"]:
            raise RuntimeError(f"Backup checksum mismatch: {entry['path']}")


def verify_archive(archive: Path) -> None:
    with tarfile.open(archive, "r:gz") as handle:
        members = [member for member in handle.getmembers() if member.isfile()]
        names = {member.name for member in members}
        manifest_name = next((name for name in names if name.endswith("/manifest.json")), "")
        if not manifest_name:
            raise RuntimeError("Backup archive does not contain manifest.json")
        manifest_file = handle.extractfile(manifest_name)
        if manifest_file is None:
            raise RuntimeError("Could not read backup manifest")
        manifest = json.load(manifest_file)
        prefix = manifest_name.removesuffix("manifest.json")
        for entry in manifest.get("files", []):
            name = prefix + entry["path"]
            extracted = handle.extractfile(name)
            if extracted is None:
                raise RuntimeError(f"Archive is missing {entry['path']}")
            digest = hashlib.sha256()
            for chunk in iter(lambda: extracted.read(1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != entry["sha256"]:
                raise RuntimeError(f"Archive checksum mismatch: {entry['path']}")


def prune_backups(output_dir: Path, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed = 0
    for path in output_dir.glob("floodman-voice-backup-*.tar.gz"):
        modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if modified < cutoff:
            path.unlink()
            removed += 1
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a live-safe Floodman Voice backup")
    parser.add_argument(
        "--data-dir",
        default=os.getenv("DATA_DIR", "/home/container/data"),
        help="Persistent Floodman data directory",
    )
    parser.add_argument(
        "--config-dir",
        default=os.getenv("CONFIG_DIR", ""),
        help="Configuration directory, default DATA_DIR/config",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Backup destination, default DATA_DIR/backups",
    )
    parser.add_argument(
        "--include-media",
        action="store_true",
        help="Include uploads and recordings, which can contain customer PII",
    )
    parser.add_argument(
        "--include-runtime-secrets",
        action="store_true",
        help="Include DATA_DIR/runtime.env; encrypt and tightly restrict the resulting archive",
    )
    parser.add_argument("--retention-days", type=int, default=30)
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser().resolve()
    config_dir = (
        Path(args.config_dir).expanduser().resolve()
        if args.config_dir
        else data_dir / "config"
    )
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else data_dir / "backups"
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_name = f"floodman-voice-backup-{timestamp}"
    final_archive = output_dir / f"{backup_name}.tar.gz"
    if final_archive.exists():
        raise RuntimeError(f"Backup already exists: {final_archive}")

    with tempfile.TemporaryDirectory(prefix="floodman-backup-") as temp_dir:
        staging_root = Path(temp_dir) / backup_name
        staging_root.mkdir(mode=0o700)
        included: list[str] = []

        database_sources = [
            (
                Path(os.getenv("DATABASE_PATH", data_dir / "floodman-voice.sqlite3")),
                Path("databases/floodman-voice.sqlite3"),
            ),
            (
                Path(os.getenv("AGENTS_DB_PATH", data_dir / "ava/operator/agents.db")),
                Path("databases/ava-agents.db"),
            ),
            (
                Path(os.getenv("CALL_HISTORY_DB_PATH", data_dir / "ava/call_history.db")),
                Path("databases/ava-call-history.db"),
            ),
        ]
        for source, relative in database_sources:
            if sqlite_backup(source, staging_root / relative):
                included.append(str(relative))

        if copy_tree(config_dir, staging_root / "config"):
            included.append("config/")
        if copy_file(data_dir / "twilio/provisioning.json", staging_root / "twilio/provisioning.json"):
            included.append("twilio/provisioning.json")
        if args.include_runtime_secrets and copy_file(
            data_dir / "runtime.env", staging_root / "secrets/runtime.env"
        ):
            included.append("secrets/runtime.env")
        if args.include_media:
            for folder in ("uploads", "recordings"):
                if copy_tree(data_dir / folder, staging_root / folder):
                    included.append(f"{folder}/")

        files: list[dict[str, Any]] = []
        for path in sorted(staging_root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                relative = path.relative_to(staging_root)
                files.append(
                    {
                        "path": str(relative),
                        "size": path.stat().st_size,
                        "sha256": sha256(path),
                    }
                )

        manifest: dict[str, Any] = {
            "format": 1,
            "application": "Floodman Operations Voice AIO",
            "application_version": VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_dir": str(data_dir),
            "includes_media": args.include_media,
            "includes_runtime_secrets": args.include_runtime_secrets,
            "included_sections": included,
            "files": files,
            "restore_note": (
                "Restore only into a stopped instance. Keep external runtime and Twilio provisioning "
                "environment files in a separate encrypted secret backup."
            ),
        }
        manifest_path = staging_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        verify_staging(staging_root, manifest)

        temporary_archive = output_dir / f".{backup_name}.tar.gz.tmp"
        try:
            with tarfile.open(temporary_archive, "w:gz", compresslevel=6) as handle:
                handle.add(staging_root, arcname=backup_name, recursive=True)
            temporary_archive.chmod(0o600)
            verify_archive(temporary_archive)
            temporary_archive.replace(final_archive)
        finally:
            temporary_archive.unlink(missing_ok=True)

    final_archive.chmod(0o600)
    removed = prune_backups(output_dir, args.retention_days)
    print(
        json.dumps(
            {
                "ok": True,
                "archive": str(final_archive),
                "sha256": sha256(final_archive),
                "size_bytes": final_archive.stat().st_size,
                "pruned": removed,
                "includes_media": args.include_media,
                "includes_runtime_secrets": args.include_runtime_secrets,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
