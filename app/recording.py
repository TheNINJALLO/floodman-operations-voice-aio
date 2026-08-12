"""Call recording manager for Floodman Operations Voice AIO.

Handles safe filename generation, SHA-256 finalization, path traversal
prevention, Range-capable streaming, retention cleanup, and payment-segment
protection.  Never blocks the call path — all failures are logged and a
failure record is written to the database rather than raising an exception
that would drop the call.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

# Matches only safe characters for embedding in filesystem paths.
_SAFE_PHONE_RE = re.compile(r"[^0-9+]")
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")

# MIME types keyed by format extension.
MIME_TYPES: dict[str, str] = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "gsm": "audio/gsm",
    "ulaw": "audio/basic",
    "alaw": "audio/basic",
}

_CHUNK = 64 * 1024  # 64 KiB streaming chunk


def safe_phone(raw: str) -> str:
    """Strip everything except digits and '+'; limit to 20 chars."""
    return _SAFE_PHONE_RE.sub("", raw)[:20] or "unknown"


def safe_id(raw: str) -> str:
    """Strip unsafe characters from an Asterisk unique-ID or UUID."""
    return _SAFE_ID_RE.sub("", raw)[:64] or "unknown"


def recording_filename(
    *,
    asterisk_unique_id: str,
    direction: str,
    caller_number: str,
    fmt: str = "wav",
    timestamp: float | None = None,
) -> str:
    """Return a safe, unique recording filename.

    Format: ``<ts>_<sanitized-uid>_<direction>_<masked-caller>.<ext>``

    The caller number is masked to the last four digits so the filename
    does not expose a full E.164 number in directory listings or logs.
    """
    ts = int(timestamp or time.time())
    uid = safe_id(asterisk_unique_id)
    masked = _mask_phone(caller_number)
    direction_safe = "in" if "in" in direction.lower() else "out"
    ext = re.sub(r"[^a-z0-9]", "", fmt.lower()) or "wav"
    return f"{ts}_{uid}_{direction_safe}_{masked}.{ext}"


def _mask_phone(number: str) -> str:
    """Return only the last four digits of a phone number for safe filenames."""
    digits = re.sub(r"[^0-9]", "", number)
    if len(digits) >= 4:
        return "x" * (len(digits) - 4) + digits[-4:]
    return digits or "unknown"


def resolve_recording_path(
    storage_dir: Path,
    filename: str,
) -> Path:
    """Resolve *filename* inside *storage_dir* with traversal protection.

    Raises ``ValueError`` if the resolved path escapes the storage directory
    or if it is a symlink that points outside it.
    """
    storage_dir = storage_dir.resolve()
    # Reject any filename that contains a path separator or is absolute.
    if os.sep in filename or (os.altsep and os.altsep in filename):
        raise ValueError(f"Path traversal rejected: {filename!r}")
    if "/" in filename or "\\" in filename:
        raise ValueError(f"Path traversal rejected: {filename!r}")
    candidate = (storage_dir / filename).resolve()
    if not str(candidate).startswith(str(storage_dir) + os.sep) and candidate != storage_dir:
        raise ValueError(f"Path traversal rejected: {filename!r}")
    # Reject symlinks that escape the storage directory.
    if candidate.is_symlink():
        link_target = candidate.resolve()
        if not str(link_target).startswith(str(storage_dir) + os.sep):
            raise ValueError(f"Symlink traversal rejected: {filename!r}")
    return candidate


def compute_sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of *path*, or empty string on error."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            while chunk := fh.read(_CHUNK):
                h.update(chunk)
        return h.hexdigest()
    except OSError as exc:
        logger.warning("sha256 failed for %s: %s", path, exc)
        return ""


def file_mime(fmt: str) -> str:
    return MIME_TYPES.get(fmt.lower(), "audio/wav")


async def stream_recording(
    path: Path,
    *,
    start: int = 0,
    end: int | None = None,
) -> AsyncIterator[bytes]:
    """Async generator that yields chunks of a recording file.

    Supports HTTP Range semantics: *start* and *end* are inclusive byte
    offsets (end=None means read to EOF).
    """
    try:
        file_size = path.stat().st_size
        if end is None:
            end = file_size - 1
        end = min(end, file_size - 1)
        if start < 0 or start > end:
            return
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                yield chunk
                remaining -= len(chunk)
    except OSError as exc:
        logger.warning("stream_recording error for %s: %s", path, exc)


class RecordingManager:
    """High-level recording lifecycle manager.

    Coordinates between the Asterisk dialplan (MixMonitor), the application
    database, and the filesystem.  All operations that might fail are wrapped
    so that a recording failure never disrupts an active call.
    """

    def __init__(self, settings: Any, db: Any) -> None:
        self._settings = settings
        self._db = db

    @property
    def enabled(self) -> bool:
        return bool(self._settings.call_recording_enabled)

    @property
    def storage_dir(self) -> Path:
        return Path(self._settings.call_recording_storage_dir)

    @property
    def fmt(self) -> str:
        return self._settings.call_recording_format or "wav"

    @property
    def retention_days(self) -> int:
        return int(self._settings.call_recording_retention_days)

    def recording_path_for(
        self,
        asterisk_unique_id: str,
        direction: str,
        caller_number: str,
    ) -> Path:
        """Return the absolute Path where Asterisk should write the recording."""
        fname = recording_filename(
            asterisk_unique_id=asterisk_unique_id,
            direction=direction,
            caller_number=caller_number,
            fmt=self.fmt,
        )
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        return self.storage_dir / fname

    def mixmonitor_command(
        self,
        asterisk_unique_id: str,
        direction: str,
        caller_number: str,
        *,
        beep: bool | None = None,
    ) -> str:
        """Return the Asterisk dialplan MixMonitor() application call string.

        The returned string is suitable for embedding in an ``exten`` line, e.g.::

            same => n,MixMonitor(/recordings/123_abc_in_xxx1234.wav,b)

        Both sides of the call (TX and RX) are mixed into a single file.
        The ``b`` flag records from the beginning of the bridge.
        ``B`` records from bridge-start including any pre-bridge audio.
        """
        path = self.recording_path_for(asterisk_unique_id, direction, caller_number)
        use_beep = beep if beep is not None else self._settings.call_recording_beep_enabled
        flags = "Bb" if use_beep else "b"
        return f"MixMonitor({path},{flags})"

    def start_recording_metadata(self, req: Any) -> dict[str, Any]:
        """Write the initial recording row to the database.

        Returns the new row (or an empty dict if disabled / on error).
        """
        if not self.enabled:
            return {}
        try:
            return self._db.create_recording(req)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to create recording metadata: %s", exc)
            return {}

    def finalize(
        self,
        asterisk_unique_id: str,
        *,
        call_id: str = "",
        protected_segment: bool = False,
    ) -> dict[str, Any] | None:
        """Compute file stats and close the recording database row.

        Called from the post-hangup AGI handler.  Never raises — any error
        is recorded in the database and logged.
        """
        try:
            row = self._db.get_recording_by_asterisk_id(asterisk_unique_id)
            if not row:
                return None
            file_path_str = row.get("file_path") or ""
            if not file_path_str:
                # Try to find the file by convention if path was not stored yet.
                file_path_str = _find_recording_file(
                    self.storage_dir, asterisk_unique_id
                )
            if not file_path_str:
                logger.warning(
                    "Recording file not found for Asterisk ID %s", asterisk_unique_id
                )
                return self._db.finalize_recording(
                    asterisk_unique_id,
                    error="file_not_found",
                    retention_days=self.retention_days,
                )

            path = Path(file_path_str)
            try:
                resolve_recording_path(self.storage_dir, path.name)
            except ValueError:
                logger.error("Recording path escapes storage dir: %s", file_path_str)
                return self._db.finalize_recording(
                    asterisk_unique_id,
                    error="path_traversal",
                    retention_days=self.retention_days,
                )

            if not path.exists():
                logger.warning("Recording file missing on disk: %s", path)
                return self._db.finalize_recording(
                    asterisk_unique_id,
                    error="file_missing",
                    retention_days=self.retention_days,
                )

            file_size = path.stat().st_size
            sha = compute_sha256(path)
            mime = file_mime(self.fmt)
            # Duration: attempt to read from WAV header; fall back to 0.
            duration = _wav_duration(path) if self.fmt == "wav" else 0.0

            return self._db.finalize_recording(
                asterisk_unique_id,
                file_path=str(path),
                file_size=file_size,
                mime_type=mime,
                sha256=sha,
                duration_seconds=duration,
                status="completed",
                protected_segment=protected_segment,
                retention_days=self.retention_days,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Recording finalize error for %s: %s", asterisk_unique_id, exc)
            try:
                return self._db.finalize_recording(
                    asterisk_unique_id,
                    error=str(exc)[:200],
                    retention_days=self.retention_days,
                )
            except Exception:  # noqa: BLE001
                pass
            return None

    def run_retention_cleanup(
        self,
        *,
        dry_run: bool = False,
        before_iso: str | None = None,
    ) -> list[dict[str, Any]]:
        """Delete recordings older than retention policy.

        Recordings under legal/operational hold are always preserved.
        The metadata row is kept with status 'expired'; only the file is removed.
        Returns the list of affected rows.
        """
        expired = self._db.expire_old_recordings(dry_run=True, before_iso=before_iso)
        deleted = []
        for rec in expired:
            file_path = rec.get("file_path") or ""
            if file_path:
                path = Path(file_path)
                try:
                    resolve_recording_path(self.storage_dir, path.name)
                    if path.exists():
                        if not dry_run:
                            path.unlink()
                            logger.info(
                                "Retention cleanup: deleted %s (id=%s)",
                                path,
                                rec["id"],
                            )
                        else:
                            logger.info(
                                "Retention dry-run: would delete %s (id=%s)",
                                path,
                                rec["id"],
                            )
                except (ValueError, OSError) as exc:
                    logger.warning(
                        "Retention cleanup skipped %s: %s", file_path, exc
                    )
            deleted.append(rec)
        if not dry_run:
            self._db.expire_old_recordings(dry_run=False, before_iso=before_iso)
        return deleted


# ── Helpers ───────────────────────────────────────────────────────────────────


def _find_recording_file(storage_dir: Path, asterisk_unique_id: str) -> str:
    """Search the storage dir for a file whose name contains *asterisk_unique_id*."""
    uid = safe_id(asterisk_unique_id)
    try:
        for p in storage_dir.iterdir():
            if uid in p.name and not p.is_dir():
                return str(p)
    except OSError:
        pass
    return ""


def _wav_duration(path: Path) -> float:
    """Read WAV header to determine duration in seconds; return 0.0 on error."""
    try:
        import struct
        with path.open("rb") as f:
            header = f.read(44)
        if len(header) < 44 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return 0.0
        # fmt chunk: channels at offset 22 (2 bytes), sample_rate at 24 (4),
        # bits_per_sample at 34 (2).
        channels, = struct.unpack_from("<H", header, 22)
        sample_rate, = struct.unpack_from("<I", header, 24)
        bits_per_sample, = struct.unpack_from("<H", header, 34)
        data_size, = struct.unpack_from("<I", header, 40)
        if channels == 0 or sample_rate == 0 or bits_per_sample == 0:
            return 0.0
        bytes_per_second = sample_rate * channels * (bits_per_sample // 8)
        if bytes_per_second == 0:
            return 0.0
        return round(data_size / bytes_per_second, 2)
    except Exception:  # noqa: BLE001
        return 0.0
