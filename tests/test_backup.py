from __future__ import annotations

import json
import sqlite3
import subprocess
import tarfile
from pathlib import Path


def _make_db(path: Path, table: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(f"CREATE TABLE {table} (value TEXT NOT NULL)")
        connection.execute(f"INSERT INTO {table} VALUES ('ready')")
        connection.commit()
    finally:
        connection.close()


def test_backup_creates_verified_archive_without_secrets_or_media_by_default(
    project_root: Path, tmp_path: Path
):
    data = tmp_path / "data"
    output = tmp_path / "backups"
    _make_db(data / "floodman-voice.sqlite3", "main_state")
    _make_db(data / "ava/operator/agents.db", "agents")
    _make_db(data / "ava/call_history.db", "history")
    (data / "config").mkdir(parents=True)
    (data / "config/floodman.yaml").write_text("business:\n  name: Floodman\n")
    (data / "runtime.env").write_text("export ADMIN_TOKEN=do-not-copy\n")
    (data / "uploads").mkdir()
    (data / "uploads/customer.jpg").write_bytes(b"customer-data")

    completed = subprocess.run(
        [
            "python3",
            str(project_root / "scripts/backup.py"),
            "--data-dir",
            str(data),
            "--output-dir",
            str(output),
            "--retention-days",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    payload = json.loads(completed.stdout)
    archive = Path(payload["archive"])
    assert archive.is_file()
    assert archive.stat().st_mode & 0o077 == 0

    with tarfile.open(archive, "r:gz") as handle:
        names = handle.getnames()
        assert any(name.endswith("/manifest.json") for name in names)
        assert any(name.endswith("/databases/floodman-voice.sqlite3") for name in names)
        assert any(name.endswith("/config/floodman.yaml") for name in names)
        assert not any("runtime.env" in name for name in names)
        assert not any("customer.jpg" in name for name in names)
        manifest_name = next(name for name in names if name.endswith("/manifest.json"))
        manifest = json.load(handle.extractfile(manifest_name))
        assert manifest["includes_media"] is False
        assert manifest["includes_runtime_secrets"] is False


def test_backup_can_explicitly_include_runtime_secrets_and_media(
    project_root: Path, tmp_path: Path
):
    data = tmp_path / "data"
    output = tmp_path / "backups"
    _make_db(data / "floodman-voice.sqlite3", "main_state")
    (data / "runtime.env").write_text("export ADMIN_TOKEN=encrypted-backup-only\n")
    (data / "uploads").mkdir(parents=True)
    (data / "uploads/photo.jpg").write_bytes(b"photo")

    completed = subprocess.run(
        [
            "python3",
            str(project_root / "scripts/backup.py"),
            "--data-dir",
            str(data),
            "--output-dir",
            str(output),
            "--include-runtime-secrets",
            "--include-media",
            "--retention-days",
            "0",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    archive = Path(json.loads(completed.stdout)["archive"])
    with tarfile.open(archive, "r:gz") as handle:
        names = handle.getnames()
        assert any(name.endswith("/secrets/runtime.env") for name in names)
        assert any(name.endswith("/uploads/photo.jpg") for name in names)
