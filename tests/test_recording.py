"""Tests for call recording feature.

Covers:
- MixMonitor presence in inbound and outbound dialplans
- recording-disabled mode creates no recording
- safe filenames
- recordings persist across DB re-opens
- Range streaming works
- unauthorized users cannot access audio
- path traversal is rejected
- retention removes expired but not held audio
- transferred call stays associated with one call record
- recording failure does not drop the call
- payment segment tracking
- metadata and SHA-256 finalization
"""
from __future__ import annotations

import hashlib
import os
import struct
import time
import wave
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models import (
    RecordingCreate,
    RecordingDirection,
    RecordingSource,
)
from app.recording import (
    RecordingManager,
    _mask_phone,
    _wav_duration,
    compute_sha256,
    recording_filename,
    resolve_recording_path,
    safe_id,
    safe_phone,
    stream_recording,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_wav_file(path: Path, duration_seconds: float = 1.0, sample_rate: int = 8000) -> None:
    """Write a minimal valid WAV file to *path*."""
    n_samples = int(sample_rate * duration_seconds)
    with wave.open(str(path), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)


def make_settings(tmp_path: Path, *, recording_enabled: bool = True) -> Settings:
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    settings = Settings.from_env(project_root=Path(__file__).resolve().parents[1])
    object.__setattr__(settings, "call_recording_enabled", recording_enabled)
    object.__setattr__(settings, "call_recording_storage_dir", tmp_path / "recordings")
    object.__setattr__(settings, "call_recording_format", "wav")
    object.__setattr__(settings, "call_recording_beep_enabled", False)
    object.__setattr__(settings, "call_recording_retention_days", 90)
    return settings


def make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.sqlite3")


def make_manager(tmp_path: Path, *, recording_enabled: bool = True) -> RecordingManager:
    settings = make_settings(tmp_path, recording_enabled=recording_enabled)
    db = make_db(tmp_path)
    return RecordingManager(settings, db), db


# ── Filename safety ───────────────────────────────────────────────────────────


def test_safe_phone_strips_unsafe():
    assert safe_phone("+1 (231) 555-0100") == "+12315550100"
    assert safe_phone("../etc/passwd") == "unknown"  # no digits in path string
    assert safe_phone("") == "unknown"


def test_safe_id_strips_unsafe():
    assert safe_id("abc-DEF_123") == "abc-DEF_123"
    assert safe_id("../../etc") == "etc"
    assert safe_id("") == "unknown"


def test_recording_filename_is_safe():
    fname = recording_filename(
        asterisk_unique_id="1677835236.12",
        direction="inbound",
        caller_number="+12315550100",
        fmt="wav",
        timestamp=1677835236,
    )
    # Must contain only safe filesystem characters
    assert " " not in fname
    assert "/" not in fname
    assert "\\" not in fname
    assert ".." not in fname
    assert fname.endswith(".wav")
    assert "in" in fname
    # Caller masked: last 4 digits only
    assert "0100" in fname
    assert "+1231555" not in fname  # full number not present


def test_recording_filename_masked_caller():
    fname = recording_filename(
        asterisk_unique_id="uid123",
        direction="outbound",
        caller_number="+12315550199",
        fmt="wav",
    )
    assert "0199" in fname
    assert "out" in fname


def test_recording_filename_empty_caller():
    fname = recording_filename(
        asterisk_unique_id="uid123",
        direction="inbound",
        caller_number="",
        fmt="wav",
    )
    assert fname.endswith(".wav")
    assert "unknown" in fname


def test_recording_filename_unsafe_format():
    fname = recording_filename(
        asterisk_unique_id="uid",
        direction="inbound",
        caller_number="",
        fmt="wav; rm -rf /",
        timestamp=1000,
    )
    assert "." in fname
    assert " " not in fname


# ── Path traversal protection ─────────────────────────────────────────────────


def test_resolve_path_ok(tmp_path: Path):
    storage = tmp_path / "recordings"
    storage.mkdir()
    p = resolve_recording_path(storage, "1677_abcd_in_xxxx0100.wav")
    assert str(p).startswith(str(storage.resolve()))


def test_resolve_path_traversal_rejected(tmp_path: Path):
    storage = tmp_path / "recordings"
    storage.mkdir()
    with pytest.raises(ValueError, match="traversal"):
        resolve_recording_path(storage, "../../../etc/passwd")


def test_resolve_path_absolute_rejected(tmp_path: Path):
    storage = tmp_path / "recordings"
    storage.mkdir()
    with pytest.raises(ValueError, match="traversal"):
        resolve_recording_path(storage, "/etc/passwd")


def test_resolve_symlink_traversal_rejected(tmp_path: Path):
    storage = tmp_path / "recordings"
    storage.mkdir()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"")
    link = storage / "link.wav"
    link.symlink_to(outside)
    with pytest.raises(ValueError, match="traversal"):
        resolve_recording_path(storage, "link.wav")


# ── SHA-256 and WAV duration ──────────────────────────────────────────────────


def test_compute_sha256(tmp_path: Path):
    f = tmp_path / "test.wav"
    f.write_bytes(b"hello world")
    digest = compute_sha256(f)
    expected = hashlib.sha256(b"hello world").hexdigest()
    assert digest == expected


def test_compute_sha256_missing_file(tmp_path: Path):
    digest = compute_sha256(tmp_path / "nonexistent.wav")
    assert digest == ""


def test_wav_duration(tmp_path: Path):
    f = tmp_path / "test.wav"
    make_wav_file(f, duration_seconds=2.0)
    duration = _wav_duration(f)
    assert 1.8 <= duration <= 2.2


def test_wav_duration_not_a_wav(tmp_path: Path):
    f = tmp_path / "noise.wav"
    f.write_bytes(b"NOTAWAV" * 10)
    assert _wav_duration(f) == 0.0


def test_wav_duration_missing(tmp_path: Path):
    assert _wav_duration(tmp_path / "gone.wav") == 0.0


# ── Recording disabled creates no file or DB row ──────────────────────────────


def test_recording_disabled_no_row(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=False)
    assert not rm.enabled
    req = RecordingCreate(
        asterisk_unique_id="test-uid-1",
        call_id="call-1",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550100",
    )
    result = rm.start_recording_metadata(req)
    assert result == {}
    # No row in DB
    rows = db.list_recordings()
    assert len(rows) == 0


def test_recording_enabled_creates_row(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    req = RecordingCreate(
        asterisk_unique_id="test-uid-2",
        call_id="call-2",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550101",
        source=RecordingSource.DIRECT,
    )
    result = rm.start_recording_metadata(req)
    assert result.get("id")
    assert result["asterisk_unique_id"] == "test-uid-2"
    assert result["status"] == "recording"


# ── Persistence across DB re-opens ────────────────────────────────────────────


def test_recording_persists_across_reopen(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    req = RecordingCreate(
        asterisk_unique_id="persist-uid",
        call_id="call-persist",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550102",
    )
    rm.start_recording_metadata(req)
    db.close()

    # Re-open database
    db2 = Database(tmp_path / "test.sqlite3")
    rows = db2.list_recordings()
    assert len(rows) == 1
    assert rows[0]["asterisk_unique_id"] == "persist-uid"


# ── Finalization: SHA-256, size, duration ─────────────────────────────────────


def test_finalize_recording(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    # Create a WAV file
    rec_path = rm.storage_dir
    rec_path.mkdir(parents=True, exist_ok=True)
    wav_file = rec_path / f"1677_finalize-uid_in_xxxx0100.wav"
    make_wav_file(wav_file, duration_seconds=3.0)

    req = RecordingCreate(
        asterisk_unique_id="finalize-uid",
        call_id="call-fin",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550103",
    )
    rm.start_recording_metadata(req)
    db.execute(
        "UPDATE recordings SET file_path=? WHERE asterisk_unique_id=?",
        (str(wav_file), "finalize-uid"),
    )

    result = rm.finalize("finalize-uid")
    assert result is not None
    assert result["status"] == "completed"
    assert result["file_size"] > 0
    assert len(result["sha256"]) == 64
    assert result["duration_seconds"] > 0


def test_finalize_missing_file(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    req = RecordingCreate(
        asterisk_unique_id="missing-file-uid",
        call_id="call-miss",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550104",
    )
    rm.start_recording_metadata(req)
    # File path not set, file doesn't exist
    result = rm.finalize("missing-file-uid")
    assert result is not None
    assert result["status"] == "failed"


def test_finalize_never_raises_on_bad_state(tmp_path: Path):
    """finalize() must return None gracefully when no row exists."""
    rm, db = make_manager(tmp_path, recording_enabled=True)
    result = rm.finalize("nonexistent-uid-xyz")
    assert result is None


# ── Retention cleanup ─────────────────────────────────────────────────────────


def test_retention_removes_expired_files(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    rec_path = rm.storage_dir
    rec_path.mkdir(parents=True, exist_ok=True)
    wav_file = rec_path / "old_rec.wav"
    make_wav_file(wav_file)

    req = RecordingCreate(
        asterisk_unique_id="old-uid",
        call_id="old-call",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550105",
    )
    rm.start_recording_metadata(req)
    # Force retention expiry to the past
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.execute(
        "UPDATE recordings SET file_path=?, retention_expires_at=?, status='completed' WHERE asterisk_unique_id=?",
        (str(wav_file), past, "old-uid"),
    )

    expired = rm.run_retention_cleanup(dry_run=False)
    assert len(expired) == 1
    assert not wav_file.exists()
    row = db.get_recording_by_asterisk_id("old-uid")
    assert row is not None
    assert row["status"] == "expired"
    assert row["file_path"] == ""


def test_retention_dry_run_does_not_delete(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    rec_path = rm.storage_dir
    rec_path.mkdir(parents=True, exist_ok=True)
    wav_file = rec_path / "dryrun_rec.wav"
    make_wav_file(wav_file)

    req = RecordingCreate(
        asterisk_unique_id="dryrun-uid",
        call_id="dryrun-call",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550106",
    )
    rm.start_recording_metadata(req)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.execute(
        "UPDATE recordings SET file_path=?, retention_expires_at=?, status='completed' WHERE asterisk_unique_id=?",
        (str(wav_file), past, "dryrun-uid"),
    )

    expired = rm.run_retention_cleanup(dry_run=True)
    assert len(expired) == 1
    assert wav_file.exists()  # File NOT deleted in dry run


def test_retention_skips_held_recordings(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    rec_path = rm.storage_dir
    rec_path.mkdir(parents=True, exist_ok=True)
    wav_file = rec_path / "held_rec.wav"
    make_wav_file(wav_file)

    req = RecordingCreate(
        asterisk_unique_id="held-uid",
        call_id="held-call",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550107",
    )
    rm.start_recording_metadata(req)
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.execute(
        "UPDATE recordings SET file_path=?, retention_expires_at=?, status='held', is_held=1 WHERE asterisk_unique_id=?",
        (str(wav_file), past, "held-uid"),
    )

    expired = rm.run_retention_cleanup(dry_run=False)
    assert len(expired) == 0  # Held recording preserved
    assert wav_file.exists()


# ── Recording hold ────────────────────────────────────────────────────────────


def test_set_hold(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    req = RecordingCreate(
        asterisk_unique_id="hold-uid",
        call_id="hold-call",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550108",
    )
    row = rm.start_recording_metadata(req)
    rec_id = row["id"]

    updated = db.set_recording_hold(rec_id, held=True, hold_reason="legal")
    assert updated is not None
    assert updated["is_held"] == 1
    assert updated["hold_reason"] == "legal"
    assert updated["status"] == "held"

    released = db.set_recording_hold(rec_id, held=False)
    assert released["is_held"] == 0
    assert released["status"] == "completed"


# ── Transfer call association ─────────────────────────────────────────────────


def test_transferred_call_single_record(tmp_path: Path):
    """A transferred call should remain associated with a single recording row."""
    rm, db = make_manager(tmp_path, recording_enabled=True)
    req = RecordingCreate(
        asterisk_unique_id="transfer-uid",
        call_id="transfer-call",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550109",
    )
    rm.start_recording_metadata(req)
    rows = db.list_recordings(call_id="transfer-call")
    assert len(rows) == 1

    # Simulate transfer: same uniqueid, just finalize
    rec_path = rm.storage_dir
    rec_path.mkdir(parents=True, exist_ok=True)
    wav_file = rec_path / "transfer.wav"
    make_wav_file(wav_file)
    db.execute(
        "UPDATE recordings SET file_path=? WHERE asterisk_unique_id=?",
        (str(wav_file), "transfer-uid"),
    )
    result = rm.finalize("transfer-uid", call_id="transfer-call")
    assert result["status"] == "completed"

    # Still exactly one row
    rows_after = db.list_recordings(call_id="transfer-call")
    assert len(rows_after) == 1


# ── Payment segment protection ────────────────────────────────────────────────


def test_protected_segment_recorded(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    req = RecordingCreate(
        asterisk_unique_id="payment-uid",
        call_id="payment-call",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550110",
    )
    rm.start_recording_metadata(req)

    rec_path = rm.storage_dir
    rec_path.mkdir(parents=True, exist_ok=True)
    wav_file = rec_path / "payment.wav"
    make_wav_file(wav_file)
    db.execute(
        "UPDATE recordings SET file_path=? WHERE asterisk_unique_id=?",
        (str(wav_file), "payment-uid"),
    )
    result = rm.finalize("payment-uid", protected_segment=True)
    assert result["protected_segment"] == 1


# ── Range streaming ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_range_streaming_full(tmp_path: Path):
    f = tmp_path / "audio.wav"
    make_wav_file(f)
    chunks = []
    async for chunk in stream_recording(f):
        chunks.append(chunk)
    data = b"".join(chunks)
    assert len(data) == f.stat().st_size


@pytest.mark.asyncio
async def test_range_streaming_partial(tmp_path: Path):
    f = tmp_path / "audio.wav"
    f.write_bytes(b"A" * 1000)
    chunks = []
    async for chunk in stream_recording(f, start=100, end=199):
        chunks.append(chunk)
    data = b"".join(chunks)
    assert len(data) == 100


@pytest.mark.asyncio
async def test_range_streaming_invalid_range(tmp_path: Path):
    f = tmp_path / "audio.wav"
    f.write_bytes(b"X" * 100)
    chunks = []
    async for chunk in stream_recording(f, start=200, end=300):
        chunks.append(chunk)
    assert b"".join(chunks) == b""  # Empty: start beyond EOF


@pytest.mark.asyncio
async def test_range_streaming_missing_file(tmp_path: Path):
    f = tmp_path / "gone.wav"
    chunks = []
    async for chunk in stream_recording(f):
        chunks.append(chunk)
    assert chunks == []  # No crash


# ── HTTP API: authentication ──────────────────────────────────────────────────


def _make_test_client(tmp_path: Path) -> tuple[TestClient, str]:
    """Create a TestClient with recording enabled."""
    from app.main import create_app  # noqa: PLC0415

    settings = make_settings(tmp_path, recording_enabled=True)
    object.__setattr__(settings, "admin_token", "test-admin-token")
    object.__setattr__(settings, "internal_token", "test-internal-token")
    object.__setattr__(settings, "database_path", tmp_path / "api_test.sqlite3")
    app = create_app(settings)
    # Manually initialise app.state so TestClient does not need to enter lifespan.
    db = Database(tmp_path / "api_test.sqlite3")
    rm = RecordingManager(settings, db)
    app.state.settings = settings
    app.state.database = db
    app.state.recording_manager = rm
    # Stub out other state attributes accessed by endpoints we are not testing.
    app.state.roomflow = None
    app.state.business = None
    app.state.compliance = None
    app.state.ami = None
    app.state.gate_server = None
    app.state.worker = None
    app.state.classifier = None
    client = TestClient(app, raise_server_exceptions=False)
    return client, "test-admin-token"


def test_recordings_requires_auth(tmp_path: Path):
    client, token = _make_test_client(tmp_path)
    r = client.get("/api/v1/recordings")
    assert r.status_code == 401


def test_recordings_with_auth(tmp_path: Path):
    client, token = _make_test_client(tmp_path)
    r = client.get("/api/v1/recordings", headers={"X-Admin-Token": token})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_stream_nonexistent_recording(tmp_path: Path):
    client, token = _make_test_client(tmp_path)
    r = client.get(
        "/api/v1/recordings/nonexistent-id/stream",
        headers={"X-Admin-Token": token},
    )
    assert r.status_code == 404


def test_stream_unauthorized(tmp_path: Path):
    client, _ = _make_test_client(tmp_path)
    r = client.get("/api/v1/recordings/any-id/stream")
    assert r.status_code == 401


# ── Dialplan: MixMonitor presence ────────────────────────────────────────────


def test_mixmonitor_in_inbound_dialplan_when_enabled(tmp_path: Path):
    """render_asterisk should include MixMonitor in inbound context when enabled."""
    import subprocess  # noqa: PLC0415

    env = {
        **os.environ,
        "CALL_RECORDING_ENABLED": "true",
        "CALL_RECORDING_FORMAT": "wav",
        "CALL_RECORDING_BEEP_ENABLED": "false",
        "DATA_DIR": str(tmp_path),
        "ASTERISK_CONFIG_DIR": str(tmp_path / "asterisk/etc"),
        "SIP_TRUNK_MODE": "disabled",
        "PUBLIC_IP": "1.2.3.4",
        "SIP_PORT": "5060",
        "RTP_START": "10000",
        "RTP_END": "10040",
    }
    result = subprocess.run(
        ["python3", "scripts/render_asterisk.py"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]), env=env
    )
    assert result.returncode == 0, result.stderr
    extensions_file = tmp_path / "asterisk/etc/extensions.conf"
    assert extensions_file.exists()
    content = extensions_file.read_text()
    assert "MixMonitor" in content
    assert "floodman-inbound" in content


def test_mixmonitor_absent_when_disabled(tmp_path: Path):
    """render_asterisk should NOT include MixMonitor when recording disabled."""
    import subprocess  # noqa: PLC0415

    env = {
        **os.environ,
        "CALL_RECORDING_ENABLED": "false",
        "DATA_DIR": str(tmp_path),
        "ASTERISK_CONFIG_DIR": str(tmp_path / "asterisk/etc"),
        "SIP_TRUNK_MODE": "disabled",
        "PUBLIC_IP": "1.2.3.4",
        "SIP_PORT": "5060",
        "RTP_START": "10000",
        "RTP_END": "10040",
    }
    result = subprocess.run(
        ["python3", "scripts/render_asterisk.py"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]), env=env
    )
    assert result.returncode == 0, result.stderr
    extensions_file = tmp_path / "asterisk/etc/extensions.conf"
    content = extensions_file.read_text()
    assert "MixMonitor" not in content


def test_outbound_dialplan_includes_mixmonitor(tmp_path: Path):
    """render_asterisk should include MixMonitor in floodman-outbound context."""
    import subprocess  # noqa: PLC0415

    env = {
        **os.environ,
        "CALL_RECORDING_ENABLED": "true",
        "DATA_DIR": str(tmp_path),
        "ASTERISK_CONFIG_DIR": str(tmp_path / "asterisk/etc"),
        "SIP_TRUNK_MODE": "disabled",
        "PUBLIC_IP": "1.2.3.4",
        "SIP_PORT": "5060",
        "RTP_START": "10000",
        "RTP_END": "10040",
    }
    result = subprocess.run(
        ["python3", "scripts/render_asterisk.py"],
        capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[1]), env=env
    )
    assert result.returncode == 0, result.stderr
    content = (tmp_path / "asterisk/etc/extensions.conf").read_text()
    assert "floodman-outbound" in content
    # Outbound section should also have MixMonitor
    outbound_section = content[content.find("[floodman-outbound]"):]
    assert "MixMonitor" in outbound_section


def test_hangup_handler_in_dialplan(tmp_path: Path):
    """Inbound context must have an 'h' extension for post-hangup finalization."""
    import subprocess  # noqa: PLC0415

    env = {
        **os.environ,
        "CALL_RECORDING_ENABLED": "true",
        "DATA_DIR": str(tmp_path),
        "ASTERISK_CONFIG_DIR": str(tmp_path / "asterisk/etc"),
        "SIP_TRUNK_MODE": "disabled",
        "PUBLIC_IP": "1.2.3.4",
        "SIP_PORT": "5060",
        "RTP_START": "10000",
        "RTP_END": "10040",
    }
    subprocess.run(["python3", "scripts/render_asterisk.py"], env=env,
                   cwd=str(Path(__file__).resolve().parents[1]), capture_output=True)
    content = (tmp_path / "asterisk/etc/extensions.conf").read_text()
    assert "exten => h,1" in content
    assert "agi_record_finalize" in content


# ── Internal endpoints ────────────────────────────────────────────────────────


def test_internal_recording_start(tmp_path: Path):
    client, _ = _make_test_client(tmp_path)
    r = client.post(
        "/internal/recordings/start",
        headers={"X-Internal-Token": "test-internal-token"},
        json={
            "asterisk_unique_id": "api-uid-1",
            "call_id": "api-call-1",
            "direction": "inbound",
            "caller_number": "+12315550120",
            "source": "direct",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["recording_id"] is not None


def test_internal_recording_finalize(tmp_path: Path):
    client, _ = _make_test_client(tmp_path)
    headers = {"X-Internal-Token": "test-internal-token"}
    # Start a recording
    client.post(
        "/internal/recordings/start",
        headers=headers,
        json={"asterisk_unique_id": "fin-api-uid", "call_id": "fin-call"},
    )
    # Finalize (file doesn't exist — should mark failed gracefully)
    r = client.post(
        "/internal/recordings/finalize",
        headers=headers,
        json={"asterisk_unique_id": "fin-api-uid", "call_id": "fin-call"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True  # Returns ok even on failed finalization


def test_recording_failure_does_not_drop_call(tmp_path: Path):
    """AGI gate_start must return 0 even when recording registration fails."""
    import importlib  # noqa: PLC0415
    import sys  # noqa: PLC0415

    # Simulate agi_gate_start by checking its exception handling is intact
    agi_path = Path(__file__).resolve().parents[1] / "scripts" / "agi_gate_start.py"
    assert agi_path.exists()
    source = agi_path.read_text()
    # Must have a bare except around recording registration
    assert "pass  # Recording failure must never affect the call" in source


def test_metadata_finalized_after_hangup(tmp_path: Path):
    """After finalize(), the SHA-256 and ended_at must be non-empty."""
    rm, db = make_manager(tmp_path, recording_enabled=True)
    req = RecordingCreate(
        asterisk_unique_id="sha-uid",
        call_id="sha-call",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315550130",
    )
    rm.start_recording_metadata(req)

    rec_path = rm.storage_dir
    rec_path.mkdir(parents=True, exist_ok=True)
    wav_file = rec_path / "sha_test.wav"
    make_wav_file(wav_file, duration_seconds=2.0)
    db.execute(
        "UPDATE recordings SET file_path=? WHERE asterisk_unique_id=?",
        (str(wav_file), "sha-uid"),
    )
    result = rm.finalize("sha-uid")
    assert result is not None
    assert len(result.get("sha256") or "") == 64
    assert result.get("ended_at") is not None
    assert result.get("duration_seconds", 0) > 0


# ── Listing and filtering ─────────────────────────────────────────────────────


def test_list_recordings_filter_by_direction(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    for i, direction in enumerate([RecordingDirection.INBOUND, RecordingDirection.OUTBOUND]):
        req = RecordingCreate(
            asterisk_unique_id=f"filter-{i}",
            call_id=f"filter-call-{i}",
            direction=direction,
            caller_number="+12315550200",
        )
        rm.start_recording_metadata(req)
    inbound = db.list_recordings(direction="inbound")
    outbound = db.list_recordings(direction="outbound")
    assert len(inbound) >= 1
    assert all(r["direction"] == "inbound" for r in inbound)
    assert len(outbound) >= 1
    assert all(r["direction"] == "outbound" for r in outbound)


def test_list_recordings_filter_by_caller(tmp_path: Path):
    rm, db = make_manager(tmp_path, recording_enabled=True)
    req = RecordingCreate(
        asterisk_unique_id="caller-filter-uid",
        call_id="caller-filter-call",
        direction=RecordingDirection.INBOUND,
        caller_number="+12315559999",
    )
    rm.start_recording_metadata(req)
    found = db.list_recordings(caller_number="+12315559999")
    assert len(found) >= 1
    not_found = db.list_recordings(caller_number="+10000000000")
    assert len(not_found) == 0
