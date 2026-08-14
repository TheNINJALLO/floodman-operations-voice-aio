from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from app.models import GateDecision, GateRegistration, JobStatus, OutboundJobCreate


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _decode_json(raw: Any, fallback: Any) -> Any:
    if raw in (None, ""):
        return fallback
    try:
        return json.loads(str(raw))
    except (json.JSONDecodeError, TypeError, ValueError):
        return fallback


SCHEMA = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS gate_sessions (
    gate_uuid TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    caller_number TEXT,
    caller_name TEXT,
    did TEXT,
    trunk TEXT,
    source_hint TEXT,
    direction TEXT NOT NULL DEFAULT 'inbound',
    state TEXT NOT NULL,
    classification TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    transcript TEXT NOT NULL DEFAULT '',
    opening_transcript TEXT NOT NULL DEFAULT '',
    announcement_detected INTEGER NOT NULL DEFAULT 0,
    agent TEXT,
    provider TEXT,
    reason TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_call_id ON gate_sessions(call_id);
CREATE INDEX IF NOT EXISTS idx_gate_phone ON gate_sessions(caller_number, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_gate_updated ON gate_sessions(updated_at DESC);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    purpose TEXT NOT NULL,
    agent TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_jobs (
    id TEXT PRIMARY KEY,
    campaign_id TEXT,
    customer_id TEXT,
    phone TEXT NOT NULL,
    purpose TEXT NOT NULL,
    agent TEXT,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL,
    scheduled_for TEXT NOT NULL,
    requested_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    consent_json TEXT,
    consent_type TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    eligibility_reason TEXT,
    ami_action_id TEXT,
    asterisk_channel TEXT,
    last_error TEXT,
    next_attempt_at TEXT,
    answered_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON outbound_jobs(status, scheduled_for, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_jobs_phone ON outbound_jobs(phone, created_at DESC);

CREATE TABLE IF NOT EXISTS suppressions (
    phone TEXT PRIMARY KEY,
    reason TEXT NOT NULL,
    categories_json TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS consent_cache (
    phone TEXT PRIMARY KEY,
    customer_id TEXT,
    transactional_voice INTEGER NOT NULL DEFAULT 0,
    marketing_voice_written INTEGER NOT NULL DEFAULT 0,
    sms INTEGER NOT NULL DEFAULT 0,
    email INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    text_version TEXT,
    consent_text TEXT,
    consented_at TEXT,
    revoked_at TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS call_events (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_call ON call_events(call_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON call_events(event_type, created_at DESC);

CREATE TABLE IF NOT EXISTS integration_outbox (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_idempotency ON integration_outbox(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_outbox_due ON integration_outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS customers (
    id TEXT PRIMARY KEY,
    external_id TEXT,
    name TEXT NOT NULL,
    phone TEXT,
    email TEXT,
    source TEXT NOT NULL DEFAULT 'voice',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_phone ON customers(phone);
CREATE INDEX IF NOT EXISTS idx_customers_external ON customers(external_id);
CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS properties (
    id TEXT PRIMARY KEY,
    external_id TEXT,
    customer_id TEXT NOT NULL,
    address TEXT NOT NULL,
    city TEXT,
    state TEXT,
    zip TEXT,
    notes TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_properties_customer ON properties(customer_id);
CREATE INDEX IF NOT EXISTS idx_properties_address ON properties(address COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_properties_zip ON properties(zip);

CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    external_id TEXT,
    customer_id TEXT NOT NULL,
    property_id TEXT,
    service TEXT,
    problem TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal',
    status TEXT NOT NULL DEFAULT 'new',
    source TEXT NOT NULL DEFAULT 'voice',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE,
    FOREIGN KEY(property_id) REFERENCES properties(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_leads_customer ON leads(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status, created_at DESC);

CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    external_id TEXT,
    customer_id TEXT NOT NULL,
    property_id TEXT,
    service TEXT,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE,
    FOREIGN KEY(property_id) REFERENCES properties(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_appointments_window ON appointments(status, start_at, end_at);
CREATE INDEX IF NOT EXISTS idx_appointments_customer ON appointments(customer_id, start_at DESC);

CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    external_id TEXT,
    customer_id TEXT NOT NULL,
    invoice_number TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    amount_due REAL NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    due_date TEXT,
    payment_url TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices(customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(invoice_number);

CREATE TABLE IF NOT EXISTS callback_tasks (
    id TEXT PRIMARY KEY,
    external_id TEXT,
    customer_id TEXT,
    call_id TEXT,
    name TEXT,
    phone TEXT NOT NULL,
    department TEXT,
    reason TEXT,
    urgency TEXT NOT NULL DEFAULT 'normal',
    preferred_time TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(customer_id) REFERENCES customers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_callbacks_status ON callback_tasks(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_callbacks_phone ON callback_tasks(phone, created_at DESC);

CREATE TABLE IF NOT EXISTS verification_sessions (
    id TEXT PRIMARY KEY,
    call_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    phone TEXT,
    method TEXT NOT NULL,
    matches_json TEXT NOT NULL DEFAULT '{}',
    verified_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verification_call ON verification_sessions(call_id, customer_id, expires_at);

CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    token_id TEXT,
    customer_id TEXT,
    call_id TEXT,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    content_type TEXT,
    size_bytes INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_customer ON uploads(customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS call_intakes (
    call_id TEXT PRIMARY KEY,
    direction TEXT NOT NULL DEFAULT 'inbound',
    caller_number TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    email_status TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    service_requested TEXT NOT NULL DEFAULT '',
    service_key TEXT NOT NULL DEFAULT '',
    service_status TEXT NOT NULL DEFAULT 'unknown',
    service_reason TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    property_context TEXT NOT NULL DEFAULT '',
    safety_summary TEXT NOT NULL DEFAULT '',
    timing_summary TEXT NOT NULL DEFAULT '',
    insurance_summary TEXT NOT NULL DEFAULT '',
    evidence_summary TEXT NOT NULL DEFAULT '',
    urgency TEXT NOT NULL DEFAULT 'normal',
    department TEXT NOT NULL DEFAULT 'estimating',
    status TEXT NOT NULL DEFAULT 'collecting',
    summary TEXT NOT NULL DEFAULT '',
    transcript_text TEXT NOT NULL DEFAULT '',
    transcript_json TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL DEFAULT '',
    customer_id TEXT NOT NULL DEFAULT '',
    property_id TEXT NOT NULL DEFAULT '',
    lead_id TEXT NOT NULL DEFAULT '',
    callback_id TEXT NOT NULL DEFAULT '',
    notification_ids_json TEXT NOT NULL DEFAULT '[]',
    notification_count INTEGER NOT NULL DEFAULT 0,
    notification_status TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_call_intakes_updated ON call_intakes(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_intakes_status ON call_intakes(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_intakes_phone ON call_intakes(phone, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_call_intakes_service ON call_intakes(service_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS recordings (
    id TEXT PRIMARY KEY,
    asterisk_unique_id TEXT NOT NULL,
    call_id TEXT NOT NULL DEFAULT '',
    direction TEXT NOT NULL DEFAULT 'inbound',
    caller_number TEXT NOT NULL DEFAULT '',
    called_number TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    campaign_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'recording',
    file_path TEXT NOT NULL DEFAULT '',
    file_size INTEGER NOT NULL DEFAULT 0,
    mime_type TEXT NOT NULL DEFAULT 'audio/wav',
    sha256 TEXT NOT NULL DEFAULT '',
    duration_seconds REAL NOT NULL DEFAULT 0,
    started_at TEXT,
    ended_at TEXT,
    retention_expires_at TEXT,
    is_held INTEGER NOT NULL DEFAULT 0,
    hold_reason TEXT NOT NULL DEFAULT '',
    disclosure_played INTEGER NOT NULL DEFAULT 0,
    disclosure_skipped_reason TEXT NOT NULL DEFAULT '',
    protected_segment INTEGER NOT NULL DEFAULT 0,
    roomflow_queued INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recordings_asterisk_id ON recordings(asterisk_unique_id);
CREATE INDEX IF NOT EXISTS idx_recordings_call_id ON recordings(call_id);
CREATE INDEX IF NOT EXISTS idx_recordings_caller ON recordings(caller_number, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recordings_status ON recordings(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recordings_started ON recordings(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_recordings_retention ON recordings(retention_expires_at, is_held, status);
"""


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._ensure_legacy_columns()

    def _ensure_legacy_columns(self) -> None:
        columns: dict[str, dict[str, str]] = {
            "outbound_jobs": {
                "answered_at": "TEXT",
            },
            "consent_cache": {
                "email": "INTEGER NOT NULL DEFAULT 0",
                "consent_text": "TEXT",
            },
        }
        with self.transaction() as conn:
            for table, additions in columns.items():
                existing = {
                    str(row["name"])
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                for name, declaration in additions.items():
                    if name not in existing:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.transaction() as conn:
            return conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    # Gate sessions -----------------------------------------------------
    def register_gate(self, registration: GateRegistration, gate_uuid: str) -> dict[str, Any]:
        now = _now()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO gate_sessions (
                    gate_uuid, call_id, caller_number, caller_name, did, trunk, source_hint,
                    direction, state, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?, ?)
                ON CONFLICT(gate_uuid) DO UPDATE SET
                    call_id=excluded.call_id,
                    caller_number=excluded.caller_number,
                    caller_name=excluded.caller_name,
                    did=excluded.did,
                    trunk=excluded.trunk,
                    source_hint=excluded.source_hint,
                    direction=excluded.direction,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    gate_uuid,
                    registration.call_id,
                    registration.caller_number,
                    registration.caller_name,
                    registration.did,
                    registration.trunk,
                    registration.source_hint,
                    registration.direction,
                    _json(registration.metadata),
                    now,
                    now,
                ),
            )
        return self.get_gate(gate_uuid) or {}

    def get_gate(self, gate_uuid: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM gate_sessions WHERE gate_uuid=?", (gate_uuid,))
        return self._decode_gate(row) if row else None

    def get_gate_by_call_id(self, call_id: str) -> dict[str, Any] | None:
        row = self.fetchone(
            "SELECT * FROM gate_sessions WHERE call_id=? ORDER BY created_at DESC LIMIT 1",
            (call_id,),
        )
        return self._decode_gate(row) if row else None

    def get_recent_gate_for_phone(self, phone: str, max_age_minutes: int = 20) -> dict[str, Any] | None:
        since = (_now_dt() - timedelta(minutes=max_age_minutes)).isoformat()
        row = self.fetchone(
            """
            SELECT * FROM gate_sessions
            WHERE caller_number=? AND created_at>=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (phone, since),
        )
        return self._decode_gate(row) if row else None

    def update_gate_state(self, gate_uuid: str, state: str, transcript: str | None = None) -> None:
        fields = ["state=?", "updated_at=?"]
        values: list[Any] = [state, _now()]
        if transcript is not None:
            fields.append("transcript=?")
            values.append(transcript)
        values.append(gate_uuid)
        self.execute(f"UPDATE gate_sessions SET {', '.join(fields)} WHERE gate_uuid=?", tuple(values))

    def save_gate_decision(self, gate_uuid: str, decision: GateDecision) -> None:
        self.execute(
            """
            UPDATE gate_sessions SET
                state=?, classification=?, confidence=?, transcript=?, opening_transcript=?,
                announcement_detected=?, agent=?, provider=?, reason=?, metadata_json=?, updated_at=?
            WHERE gate_uuid=?
            """,
            (
                decision.state.value,
                decision.call_type.value,
                decision.confidence,
                decision.transcript,
                decision.opening_transcript,
                int(decision.announcement_detected),
                decision.agent,
                decision.provider,
                decision.reason,
                _json(decision.metadata),
                _now(),
                gate_uuid,
            ),
        )

    def list_gate_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM gate_sessions ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._decode_gate(row) for row in rows]

    @staticmethod
    def _decode_gate(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["metadata"] = _decode_json(result.pop("metadata_json", "{}"), {})
        result["announcement_detected"] = bool(result.get("announcement_detected"))
        return result

    # Campaigns and outbound jobs --------------------------------------
    def create_campaign(
        self, name: str, purpose: str, agent: str, status: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        campaign_id = str(uuid.uuid4())
        now = _now()
        self.execute(
            "INSERT INTO campaigns (id,name,purpose,agent,status,config_json,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (campaign_id, name, purpose, agent, status, _json(config), now, now),
        )
        return self.get_campaign(campaign_id) or {}

    def get_campaign(self, campaign_id: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM campaigns WHERE id=?", (campaign_id,))
        if not row:
            return None
        row["config"] = _decode_json(row.pop("config_json", "{}"), {})
        return row

    def update_campaign(self, campaign_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"name", "status", "agent", "config"}
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed or value is None:
                continue
            column = "config_json" if key == "config" else key
            sets.append(f"{column}=?")
            values.append(_json(value) if key == "config" else value)
        if not sets:
            return self.get_campaign(campaign_id)
        sets.append("updated_at=?")
        values.extend([_now(), campaign_id])
        self.execute(f"UPDATE campaigns SET {', '.join(sets)} WHERE id=?", tuple(values))
        return self.get_campaign(campaign_id)

    def list_campaigns(self) -> list[dict[str, Any]]:
        rows = self.fetchall(
            """
            SELECT c.*,
                   COUNT(j.id) AS total_jobs,
                   SUM(CASE WHEN j.status IN ('pending','retry','dialing','answered') THEN 1 ELSE 0 END) AS open_jobs,
                   SUM(CASE WHEN j.status='completed' THEN 1 ELSE 0 END) AS completed_jobs,
                   SUM(CASE WHEN j.status='blocked' THEN 1 ELSE 0 END) AS blocked_jobs
            FROM campaigns c
            LEFT JOIN outbound_jobs j ON j.campaign_id=c.id
            GROUP BY c.id
            ORDER BY c.created_at DESC
            """
        )
        for row in rows:
            row["config"] = _decode_json(row.pop("config_json", "{}"), {})
            for key in ("total_jobs", "open_jobs", "completed_jobs", "blocked_jobs"):
                row[key] = int(row.get(key) or 0)
        return rows

    def has_open_campaign_job(self, campaign_id: str, phone: str) -> bool:
        row = self.fetchone(
            """
            SELECT 1 AS found FROM outbound_jobs
            WHERE campaign_id=? AND phone=?
              AND status IN ('pending','retry','dialing','answered','blocked')
            LIMIT 1
            """,
            (campaign_id, phone),
        )
        return bool(row)

    def create_outbound_job(self, request: OutboundJobCreate, agent: str) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = _now()
        consent_json = request.consent.model_dump_json() if request.consent else None
        self.execute(
            """
            INSERT INTO outbound_jobs (
                id,campaign_id,customer_id,phone,purpose,agent,timezone,status,scheduled_for,
                requested_at,max_attempts,consent_json,payload_json,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)
            """,
            (
                job_id,
                request.campaign_id or None,
                request.customer_id,
                request.phone,
                request.purpose.value,
                agent,
                request.timezone,
                _iso(request.scheduled_for),
                _iso(request.requested_at),
                request.max_attempts,
                consent_json,
                _json(request.payload),
                now,
                now,
            ),
        )
        return self.get_outbound_job(job_id) or {}

    def get_outbound_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM outbound_jobs WHERE id=?", (job_id,))
        return self._decode_job(row) if row else None

    def list_outbound_jobs(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM outbound_jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._decode_job(row) for row in rows]

    def due_outbound_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        now = _now()
        rows = self.fetchall(
            """
            SELECT j.* FROM outbound_jobs j
            LEFT JOIN campaigns c ON c.id=j.campaign_id
            WHERE j.status IN ('pending','retry')
              AND j.scheduled_for <= ?
              AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?)
              AND (j.campaign_id IS NULL OR c.status='active')
            ORDER BY j.scheduled_for ASC LIMIT ?
            """,
            (now, now, limit),
        )
        return [self._decode_job(row) for row in rows]

    def stale_dialing_jobs(self, older_than_seconds: int, limit: int = 50) -> list[dict[str, Any]]:
        cutoff = (_now_dt() - timedelta(seconds=max(60, older_than_seconds))).isoformat()
        rows = self.fetchall(
            """
            SELECT * FROM outbound_jobs
            WHERE status IN ('dialing','answered') AND updated_at<=?
            ORDER BY updated_at ASC LIMIT ?
            """,
            (cutoff, limit),
        )
        return [self._decode_job(row) for row in rows]

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "status",
            "attempts",
            "consent_type",
            "eligibility_reason",
            "ami_action_id",
            "asterisk_channel",
            "last_error",
            "next_attempt_at",
            "answered_at",
            "completed_at",
            "scheduled_for",
        }
        sets: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"Unsupported outbound job field: {key}")
            sets.append(f"{key}=?")
            values.append(_iso(value) if isinstance(value, datetime) else value)
        if not sets:
            return self.get_outbound_job(job_id)
        sets.append("updated_at=?")
        values.extend([_now(), job_id])
        self.execute(f"UPDATE outbound_jobs SET {', '.join(sets)} WHERE id=?", tuple(values))
        return self.get_outbound_job(job_id)

    def retry_job(self, job_id: str, scheduled_for: datetime | None = None) -> dict[str, Any] | None:
        row = self.get_outbound_job(job_id)
        if not row:
            return None
        return self.update_job(
            job_id,
            status=JobStatus.RETRY.value,
            scheduled_for=scheduled_for or _now_dt(),
            next_attempt_at=None,
            last_error=None,
        )

    def complete_outbound_job(
        self, job_id: str, outcome: str, call_id: str = ""
    ) -> dict[str, Any] | None:
        row = self.get_outbound_job(job_id)
        if not row:
            return None
        result = self.update_job(
            job_id,
            status=JobStatus.COMPLETED.value,
            completed_at=_now(),
            eligibility_reason=outcome,
        )
        if call_id:
            self.add_call_event(
                call_id,
                "outbound",
                "job_completed",
                {
                    "job_id": job_id,
                    "campaign_id": row.get("campaign_id") or "",
                    "outcome": outcome,
                    "phone": row.get("phone") or "",
                    "correlation": "aava_lead_id",
                },
            )
        return result

    def complete_latest_job_for_phone(
        self, phone: str, outcome: str, call_id: str = ""
    ) -> dict[str, Any] | None:
        row = self.fetchone(
            """
            SELECT id FROM outbound_jobs
            WHERE phone=? AND status IN ('dialing','answered')
            ORDER BY updated_at DESC LIMIT 1
            """,
            (phone,),
        )
        if not row:
            return None
        result = self.update_job(
            str(row["id"]),
            status=JobStatus.COMPLETED.value,
            completed_at=_now(),
            eligibility_reason=outcome,
        )
        if call_id:
            self.add_call_event(
                call_id,
                "outbound",
                "job_completed",
                {"job_id": row["id"], "outcome": outcome, "phone": phone},
            )
        return result

    @staticmethod
    def _decode_job(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        result["payload"] = _decode_json(result.pop("payload_json", "{}"), {})
        result["consent"] = _decode_json(result.pop("consent_json", None), None)
        return result

    def count_recent_attempts(self, phone: str, purpose: str, since: datetime) -> int:
        row = self.fetchone(
            """
            SELECT COALESCE(SUM(attempts),0) AS total FROM outbound_jobs
            WHERE phone=? AND purpose=? AND created_at>=?
            """,
            (phone, purpose, _iso(since)),
        )
        return int(row["total"] if row else 0)

    def last_live_contact(self, phone: str) -> datetime | None:
        row = self.fetchone(
            """
            SELECT created_at FROM call_events
            WHERE direction='outbound' AND event_type='live_contact'
              AND json_extract(payload_json,'$.phone')=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (phone,),
        )
        if not row:
            return None
        try:
            return datetime.fromisoformat(str(row["created_at"]))
        except ValueError:
            return None

    # Consent and suppression ------------------------------------------
    def upsert_consent(self, value: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        phone = str(value.get("phone", ""))
        self.execute(
            """
            INSERT INTO consent_cache (
                phone,customer_id,transactional_voice,marketing_voice_written,sms,email,source,
                text_version,consent_text,consented_at,revoked_at,raw_json,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(phone) DO UPDATE SET
                customer_id=excluded.customer_id,
                transactional_voice=excluded.transactional_voice,
                marketing_voice_written=excluded.marketing_voice_written,
                sms=excluded.sms,
                email=excluded.email,
                source=excluded.source,
                text_version=excluded.text_version,
                consent_text=excluded.consent_text,
                consented_at=excluded.consented_at,
                revoked_at=excluded.revoked_at,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                phone,
                value.get("customer_id", ""),
                int(bool(value.get("transactional_voice"))),
                int(bool(value.get("marketing_voice_written"))),
                int(bool(value.get("sms"))),
                int(bool(value.get("email"))),
                value.get("source", ""),
                value.get("text_version", ""),
                value.get("consent_text", ""),
                _iso(value.get("consented_at")),
                _iso(value.get("revoked_at")),
                _json(value.get("raw", {})),
                now,
            ),
        )
        return self.get_consent(phone) or {}

    def get_consent(self, phone: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM consent_cache WHERE phone=?", (phone,))
        if not row:
            return None
        for key in ("transactional_voice", "marketing_voice_written", "sms", "email"):
            row[key] = bool(row.get(key))
        row["raw"] = _decode_json(row.pop("raw_json", "{}"), {})
        return row

    def list_consents(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM consent_cache ORDER BY updated_at DESC LIMIT ?", (limit,))
        result: list[dict[str, Any]] = []
        for row in rows:
            for key in ("transactional_voice", "marketing_voice_written", "sms", "email"):
                row[key] = bool(row.get(key))
            row["raw"] = _decode_json(row.pop("raw_json", "{}"), {})
            result.append(row)
        return result

    def revoke_consent(self, phone: str, categories: list[str] | None = None) -> dict[str, Any] | None:
        row = self.get_consent(phone)
        if not row:
            return None
        categories_set = {str(value).strip().lower() for value in (categories or ["all"])}
        revoked_at = _now()
        if "all" in categories_set or "transactional" in categories_set:
            row["transactional_voice"] = False
        if "all" in categories_set or "marketing" in categories_set:
            row["marketing_voice_written"] = False
        if "all" in categories_set or "sms" in categories_set:
            row["sms"] = False
        if "all" in categories_set or "email" in categories_set:
            row["email"] = False
        raw = dict(row.get("raw") or {})
        revocations = list(raw.get("revocations") or [])
        revocations.append({"categories": sorted(categories_set), "revoked_at": revoked_at})
        raw["revocations"] = revocations[-100:]
        row["raw"] = raw
        all_revoked = not any(
            bool(row.get(key))
            for key in ("transactional_voice", "marketing_voice_written", "sms", "email")
        )
        row["revoked_at"] = revoked_at if "all" in categories_set or all_revoked else None
        return self.upsert_consent(row)

    def suppress(self, phone: str, reason: str, categories: list[str], source: str) -> dict[str, Any]:
        now = _now()
        normalized = sorted({str(item).strip().lower() for item in categories if str(item).strip()}) or ["all"]
        self.execute(
            """
            INSERT INTO suppressions (phone,reason,categories_json,source,created_at,updated_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(phone) DO UPDATE SET reason=excluded.reason,
                categories_json=excluded.categories_json,source=excluded.source,updated_at=excluded.updated_at
            """,
            (phone, reason, _json(normalized), source, now, now),
        )
        self.execute(
            "UPDATE outbound_jobs SET status='canceled', last_error='suppressed', updated_at=? WHERE phone=? AND status IN ('pending','retry')",
            (now, phone),
        )
        return self.get_suppression(phone) or {}

    def delete_suppression(self, phone: str) -> bool:
        cursor = self.execute("DELETE FROM suppressions WHERE phone=?", (phone,))
        return cursor.rowcount > 0

    def get_suppression(self, phone: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM suppressions WHERE phone=?", (phone,))
        if not row:
            return None
        row["categories"] = _decode_json(row.pop("categories_json", "[]"), [])
        return row

    def list_suppressions(self) -> list[dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM suppressions ORDER BY updated_at DESC")
        for row in rows:
            row["categories"] = _decode_json(row.pop("categories_json", "[]"), [])
        return rows

    # Durable inbound intake snapshots --------------------------------
    def upsert_call_intake(
        self,
        call_id: str,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        from app.intake import merge_intake_snapshot

        call_id = str(call_id or "").strip()
        if not call_id:
            raise ValueError("call_id is required")
        existing = self.get_call_intake(call_id) or {}
        merged = merge_intake_snapshot(existing, {**value, "call_id": call_id})
        now = _now()
        merged["call_id"] = call_id
        merged["created_at"] = str(existing.get("created_at") or now)
        merged["updated_at"] = now

        columns = (
            "call_id",
            "direction",
            "caller_number",
            "name",
            "phone",
            "email",
            "email_status",
            "address",
            "service_requested",
            "service_key",
            "service_status",
            "service_reason",
            "description",
            "property_context",
            "safety_summary",
            "timing_summary",
            "insurance_summary",
            "evidence_summary",
            "urgency",
            "department",
            "status",
            "summary",
            "transcript_text",
            "transcript_json",
            "outcome",
            "customer_id",
            "property_id",
            "lead_id",
            "callback_id",
            "notification_ids_json",
            "notification_count",
            "notification_status",
            "metadata_json",
            "created_at",
            "updated_at",
            "completed_at",
        )
        text_defaults = {
            "direction": "inbound",
            "caller_number": "",
            "name": "",
            "phone": "",
            "email": "",
            "email_status": "",
            "address": "",
            "service_requested": "",
            "service_key": "",
            "service_status": "unknown",
            "service_reason": "",
            "description": "",
            "property_context": "",
            "safety_summary": "",
            "timing_summary": "",
            "insurance_summary": "",
            "evidence_summary": "",
            "urgency": "normal",
            "department": "estimating",
            "status": "collecting",
            "summary": "",
            "transcript_text": "",
            "outcome": "",
            "customer_id": "",
            "property_id": "",
            "lead_id": "",
            "callback_id": "",
            "notification_status": "",
            "completed_at": None,
        }
        record: dict[str, Any] = {}
        for column in columns:
            if column == "transcript_json":
                record[column] = _json(merged.get("transcript") or [])
            elif column == "notification_ids_json":
                record[column] = _json(merged.get("notification_ids") or [])
            elif column == "metadata_json":
                record[column] = _json(merged.get("metadata") or {})
            elif column == "notification_count":
                record[column] = int(merged.get(column) or 0)
            elif column in {"call_id", "created_at", "updated_at"}:
                record[column] = merged[column]
            else:
                default = text_defaults.get(column, "")
                raw = merged.get(column, default)
                record[column] = default if raw is None and default is not None else raw

        update_columns = [
            column
            for column in columns
            if column not in {"call_id", "created_at"}
        ]
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{column}=excluded.{column}" for column in update_columns
        )
        self.execute(
            f"INSERT INTO call_intakes ({','.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(call_id) DO UPDATE SET {updates}",
            tuple(record[column] for column in columns),
        )
        return self.get_call_intake(call_id) or {}

    def get_call_intake(self, call_id: str) -> dict[str, Any] | None:
        row = self.fetchone(
            "SELECT * FROM call_intakes WHERE call_id=?",
            (str(call_id or ""),),
        )
        if not row:
            return None
        row["transcript"] = _decode_json(row.pop("transcript_json", "[]"), [])
        row["notification_ids"] = _decode_json(
            row.pop("notification_ids_json", "[]"),
            [],
        )
        row["metadata"] = _decode_json(row.pop("metadata_json", "{}"), {})
        return row

    def list_call_intakes(
        self,
        *,
        limit: int = 200,
        status: str = "",
        service_status: str = "",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if service_status:
            clauses.append("service_status=?")
            params.append(service_status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(int(limit), 1000)))
        rows = self.fetchall(
            f"""
            SELECT call_id,direction,caller_number,name,phone,email,email_status,
                   address,service_requested,service_key,service_status,
                   service_reason,description,property_context,safety_summary,
                   timing_summary,insurance_summary,evidence_summary,urgency,
                   department,status,summary,outcome,customer_id,property_id,
                   lead_id,callback_id,notification_count,notification_status,
                   created_at,updated_at,completed_at
            FROM call_intakes
            {where}
            ORDER BY updated_at DESC LIMIT ?
            """,
            tuple(params),
        )
        return rows

    # Events and integration outbox ------------------------------------
    def add_call_event(
        self, call_id: str, direction: str, event_type: str, payload: dict[str, Any]
    ) -> str:
        event_id = str(uuid.uuid4())
        self.execute(
            "INSERT INTO call_events (id,call_id,direction,event_type,payload_json,created_at) VALUES (?,?,?,?,?,?)",
            (event_id, call_id, direction, event_type, _json(payload), _now()),
        )
        return event_id

    def list_call_events(self, limit: int = 200, call_id: str = "") -> list[dict[str, Any]]:
        if call_id:
            rows = self.fetchall(
                "SELECT * FROM call_events WHERE call_id=? ORDER BY created_at DESC LIMIT ?",
                (call_id, limit),
            )
        else:
            rows = self.fetchall("SELECT * FROM call_events ORDER BY created_at DESC LIMIT ?", (limit,))
        for row in rows:
            row["payload"] = _decode_json(row.pop("payload_json", "{}"), {})
        return rows

    def queue_outbox(self, operation: str, payload: dict[str, Any], idempotency_key: str) -> str:
        existing = self.fetchone(
            "SELECT id FROM integration_outbox WHERE idempotency_key=?", (idempotency_key,)
        )
        if existing:
            return str(existing["id"])
        item_id = str(uuid.uuid4())
        now = _now()
        self.execute(
            """
            INSERT INTO integration_outbox
            (id,operation,payload_json,idempotency_key,status,attempts,next_attempt_at,created_at,updated_at)
            VALUES (?,?,?,?,'pending',0,?,?,?)
            """,
            (item_id, operation, _json(payload), idempotency_key, now, now, now),
        )
        return item_id

    def list_outbox(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.fetchall(
            "SELECT * FROM integration_outbox ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        for row in rows:
            row["payload"] = _decode_json(row.pop("payload_json", "{}"), {})
        return rows

    def due_outbox(self, limit: int = 25) -> list[dict[str, Any]]:
        rows = self.fetchall(
            """
            SELECT * FROM integration_outbox
            WHERE status IN ('pending','retry') AND next_attempt_at<=?
            ORDER BY created_at ASC LIMIT ?
            """,
            (_now(), limit),
        )
        for row in rows:
            row["payload"] = _decode_json(row.pop("payload_json", "{}"), {})
        return rows

    def retry_outbox(self, item_id: str) -> bool:
        cursor = self.execute(
            """
            UPDATE integration_outbox SET status='retry',next_attempt_at=?,last_error=NULL,updated_at=?
            WHERE id=?
            """,
            (_now(), _now(), item_id),
        )
        return cursor.rowcount > 0

    def mark_outbox(self, item_id: str, success: bool, error: str = "") -> None:
        row = self.fetchone("SELECT attempts FROM integration_outbox WHERE id=?", (item_id,))
        attempts = int(row["attempts"] if row else 0) + 1
        if success:
            self.execute(
                "UPDATE integration_outbox SET status='completed',attempts=?,last_error=NULL,updated_at=? WHERE id=?",
                (attempts, _now(), item_id),
            )
            return
        delay = min(3600, 15 * (2 ** min(attempts, 8)))
        next_at = (_now_dt() + timedelta(seconds=delay)).isoformat()
        status = "failed" if attempts >= 12 else "retry"
        self.execute(
            """
            UPDATE integration_outbox SET status=?,attempts=?,last_error=?,next_attempt_at=?,updated_at=?
            WHERE id=?
            """,
            (status, attempts, error[:1000], next_at, _now(), item_id),
        )

    # Local CRM ---------------------------------------------------------
    def upsert_customer(self, value: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        customer_id = str(value.get("id") or "")
        external_id = str(value.get("external_id") or "")
        phone = str(value.get("phone") or "")
        existing: dict[str, Any] | None = None
        if customer_id:
            existing = self.fetchone("SELECT id FROM customers WHERE id=?", (customer_id,))
        if not existing and external_id:
            existing = self.fetchone("SELECT id FROM customers WHERE external_id=?", (external_id,))
        if not existing and phone:
            existing = self.fetchone("SELECT id FROM customers WHERE phone=? ORDER BY updated_at DESC LIMIT 1", (phone,))
        customer_id = str(existing["id"]) if existing else customer_id or str(uuid.uuid4())
        self.execute(
            """
            INSERT INTO customers (id,external_id,name,phone,email,source,metadata_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                external_id=COALESCE(NULLIF(excluded.external_id,''),customers.external_id),
                name=COALESCE(NULLIF(excluded.name,''),customers.name),
                phone=COALESCE(NULLIF(excluded.phone,''),customers.phone),
                email=COALESCE(NULLIF(excluded.email,''),customers.email),
                source=COALESCE(NULLIF(excluded.source,''),customers.source),
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                customer_id,
                external_id,
                str(value.get("name") or "Unknown caller"),
                phone,
                str(value.get("email") or ""),
                str(value.get("source") or "voice"),
                _json(value.get("metadata") or {}),
                now,
                now,
            ),
        )
        return self.get_customer(customer_id) or {}

    def get_customer(self, customer_id: str) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM customers WHERE id=?", (customer_id,))
        return self._decode_crm(row) if row else None

    def search_customers(
        self, *, phone: str = "", name: str = "", address: str = "", limit: int = 20
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if phone:
            clauses.append("c.phone=?")
            params.append(phone)
        if name:
            clauses.append("c.name LIKE ? COLLATE NOCASE")
            params.append(f"%{name}%")
        if address:
            clauses.append("p.address LIKE ? COLLATE NOCASE")
            params.append(f"%{address}%")
        where = " AND ".join(clauses) if clauses else "1=1"
        params.append(limit)
        rows = self.fetchall(
            f"""
            SELECT DISTINCT c.* FROM customers c
            LEFT JOIN properties p ON p.customer_id=c.id
            WHERE {where}
            ORDER BY c.updated_at DESC LIMIT ?
            """,
            tuple(params),
        )
        return [self.customer_bundle(str(row["id"])) for row in rows]

    def customer_bundle(self, customer_id: str) -> dict[str, Any]:
        customer = self.get_customer(customer_id) or {}
        if not customer:
            return {}
        customer["properties"] = self.list_properties(customer_id)
        customer["leads"] = self.list_leads(customer_id, 20)
        customer["appointments"] = self.list_appointments(customer_id, 20)
        customer["invoices"] = self.list_invoices(customer_id, 20)
        customer["callbacks"] = self.list_callback_tasks(customer_id=customer_id, limit=20)
        return customer

    def list_customers(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.fetchall("SELECT * FROM customers ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [self._decode_crm(row) for row in rows]

    def upsert_property(self, value: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        property_id = str(value.get("id") or "")
        customer_id = str(value.get("customer_id") or "")
        address = str(value.get("address") or "").strip()
        existing: dict[str, Any] | None = None
        if property_id:
            existing = self.fetchone("SELECT id FROM properties WHERE id=?", (property_id,))
        if not existing and value.get("external_id"):
            existing = self.fetchone("SELECT id FROM properties WHERE external_id=?", (str(value["external_id"]),))
        if not existing and customer_id and address:
            existing = self.fetchone(
                "SELECT id FROM properties WHERE customer_id=? AND lower(address)=lower(?) LIMIT 1",
                (customer_id, address),
            )
        property_id = str(existing["id"]) if existing else property_id or str(uuid.uuid4())
        self.execute(
            """
            INSERT INTO properties
            (id,external_id,customer_id,address,city,state,zip,notes,metadata_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                external_id=COALESCE(NULLIF(excluded.external_id,''),properties.external_id),
                customer_id=excluded.customer_id,address=excluded.address,city=excluded.city,
                state=excluded.state,zip=excluded.zip,notes=excluded.notes,
                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
            """,
            (
                property_id,
                str(value.get("external_id") or ""),
                customer_id,
                address,
                str(value.get("city") or ""),
                str(value.get("state") or "MI"),
                str(value.get("zip") or ""),
                str(value.get("notes") or ""),
                _json(value.get("metadata") or {}),
                now,
                now,
            ),
        )
        row = self.fetchone("SELECT * FROM properties WHERE id=?", (property_id,))
        return self._decode_crm(row) if row else {}

    def list_properties(self, customer_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if customer_id:
            rows = self.fetchall(
                "SELECT * FROM properties WHERE customer_id=? ORDER BY updated_at DESC LIMIT ?",
                (customer_id, limit),
            )
        else:
            rows = self.fetchall("SELECT * FROM properties ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [self._decode_crm(row) for row in rows]

    def create_lead(self, value: dict[str, Any]) -> dict[str, Any]:
        lead_id = str(value.get("id") or uuid.uuid4())
        now = _now()
        self.execute(
            """
            INSERT INTO leads
            (id,external_id,customer_id,property_id,service,problem,urgency,status,source,metadata_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET external_id=excluded.external_id,customer_id=excluded.customer_id,
                property_id=excluded.property_id,service=excluded.service,problem=excluded.problem,
                urgency=excluded.urgency,status=excluded.status,source=excluded.source,
                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
            """,
            (
                lead_id,
                str(value.get("external_id") or ""),
                str(value.get("customer_id") or ""),
                str(value.get("property_id") or "") or None,
                str(value.get("service") or ""),
                str(value.get("problem") or ""),
                str(value.get("urgency") or "normal"),
                str(value.get("status") or "new"),
                str(value.get("source") or "voice"),
                _json(value.get("metadata") or {}),
                now,
                now,
            ),
        )
        row = self.fetchone("SELECT * FROM leads WHERE id=?", (lead_id,))
        return self._decode_crm(row) if row else {}

    def list_leads(self, customer_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if customer_id:
            rows = self.fetchall(
                "SELECT * FROM leads WHERE customer_id=? ORDER BY created_at DESC LIMIT ?",
                (customer_id, limit),
            )
        else:
            rows = self.fetchall("SELECT * FROM leads ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._decode_crm(row) for row in rows]

    def appointment_conflicts(self, start_at: str, end_at: str, exclude_id: str = "") -> bool:
        params: list[Any] = [end_at, start_at]
        extra = ""
        if exclude_id:
            extra = " AND id<>?"
            params.append(exclude_id)
        row = self.fetchone(
            f"""
            SELECT id FROM appointments
            WHERE status IN ('scheduled','confirmed') AND start_at<? AND end_at>?{extra}
            LIMIT 1
            """,
            tuple(params),
        )
        return bool(row)

    def create_appointment(self, value: dict[str, Any]) -> dict[str, Any]:
        appointment_id = str(value.get("id") or uuid.uuid4())
        start_at = str(value.get("start") or value.get("start_at") or "")
        end_at = str(value.get("end") or value.get("end_at") or "")
        if not start_at or not end_at:
            raise ValueError("Appointment start and end are required")
        if self.appointment_conflicts(start_at, end_at, appointment_id):
            raise ValueError("appointment_slot_conflict")
        now = _now()
        self.execute(
            """
            INSERT INTO appointments
            (id,external_id,customer_id,property_id,service,start_at,end_at,timezone,status,metadata_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET external_id=excluded.external_id,customer_id=excluded.customer_id,
                property_id=excluded.property_id,service=excluded.service,start_at=excluded.start_at,
                end_at=excluded.end_at,timezone=excluded.timezone,status=excluded.status,
                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
            """,
            (
                appointment_id,
                str(value.get("external_id") or ""),
                str(value.get("customer_id") or ""),
                str(value.get("property_id") or "") or None,
                str(value.get("service") or "inspection"),
                start_at,
                end_at,
                str(value.get("timezone") or "America/Detroit"),
                str(value.get("status") or "scheduled"),
                _json(value.get("metadata") or {}),
                now,
                now,
            ),
        )
        row = self.fetchone("SELECT * FROM appointments WHERE id=?", (appointment_id,))
        return self._decode_crm(row) if row else {}

    def reschedule_appointment(self, appointment_id: str, value: dict[str, Any]) -> dict[str, Any] | None:
        row = self.fetchone("SELECT * FROM appointments WHERE id=?", (appointment_id,))
        if not row:
            return None
        merged = self._decode_crm(row)
        merged.update(value)
        merged["id"] = appointment_id
        return self.create_appointment(merged)

    def list_appointments(self, customer_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if customer_id:
            rows = self.fetchall(
                "SELECT * FROM appointments WHERE customer_id=? ORDER BY start_at DESC LIMIT ?",
                (customer_id, limit),
            )
        else:
            rows = self.fetchall("SELECT * FROM appointments ORDER BY start_at DESC LIMIT ?", (limit,))
        return [self._decode_crm(row) for row in rows]

    def upsert_invoice(self, value: dict[str, Any]) -> dict[str, Any]:
        invoice_id = str(value.get("id") or "")
        external_id = str(value.get("external_id") or "")
        invoice_number = str(value.get("invoice_number") or "")
        existing: dict[str, Any] | None = None
        if invoice_id:
            existing = self.fetchone("SELECT id FROM invoices WHERE id=?", (invoice_id,))
        if not existing and external_id:
            existing = self.fetchone("SELECT id FROM invoices WHERE external_id=?", (external_id,))
        if not existing and invoice_number:
            existing = self.fetchone("SELECT id FROM invoices WHERE invoice_number=?", (invoice_number,))
        invoice_id = str(existing["id"]) if existing else invoice_id or str(uuid.uuid4())
        now = _now()
        self.execute(
            """
            INSERT INTO invoices
            (id,external_id,customer_id,invoice_number,status,amount_due,currency,due_date,payment_url,metadata_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET external_id=excluded.external_id,customer_id=excluded.customer_id,
                invoice_number=excluded.invoice_number,status=excluded.status,amount_due=excluded.amount_due,
                currency=excluded.currency,due_date=excluded.due_date,payment_url=excluded.payment_url,
                metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
            """,
            (
                invoice_id,
                external_id,
                str(value.get("customer_id") or ""),
                invoice_number,
                str(value.get("status") or "open"),
                float(value.get("amount_due") or 0),
                str(value.get("currency") or "USD"),
                _iso(value.get("due_date")),
                str(value.get("payment_url") or ""),
                _json(value.get("metadata") or {}),
                now,
                now,
            ),
        )
        row = self.fetchone("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        return self._decode_crm(row) if row else {}

    def get_invoice(self, invoice_id: str = "", invoice_number: str = "") -> dict[str, Any] | None:
        if invoice_id:
            row = self.fetchone("SELECT * FROM invoices WHERE id=?", (invoice_id,))
        else:
            row = self.fetchone("SELECT * FROM invoices WHERE invoice_number=?", (invoice_number,))
        return self._decode_crm(row) if row else None

    def list_invoices(self, customer_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if customer_id:
            rows = self.fetchall(
                "SELECT * FROM invoices WHERE customer_id=? ORDER BY created_at DESC LIMIT ?",
                (customer_id, limit),
            )
        else:
            rows = self.fetchall("SELECT * FROM invoices ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._decode_crm(row) for row in rows]

    def create_callback_task(self, value: dict[str, Any]) -> dict[str, Any]:
        task_id = str(value.get("id") or uuid.uuid4())
        now = _now()
        self.execute(
            """
            INSERT INTO callback_tasks
            (id,external_id,customer_id,call_id,name,phone,department,reason,urgency,preferred_time,status,metadata_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET external_id=excluded.external_id,customer_id=excluded.customer_id,
                call_id=excluded.call_id,name=excluded.name,phone=excluded.phone,department=excluded.department,
                reason=excluded.reason,urgency=excluded.urgency,preferred_time=excluded.preferred_time,
                status=excluded.status,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at
            """,
            (
                task_id,
                str(value.get("external_id") or ""),
                str(value.get("customer_id") or "") or None,
                str(value.get("call_id") or ""),
                str(value.get("name") or ""),
                str(value.get("phone") or ""),
                str(value.get("department") or "support"),
                str(value.get("reason") or ""),
                str(value.get("urgency") or "normal"),
                str(value.get("preferred_time") or ""),
                str(value.get("status") or "open"),
                _json(value.get("metadata") or {}),
                now,
                now,
            ),
        )
        row = self.fetchone("SELECT * FROM callback_tasks WHERE id=?", (task_id,))
        return self._decode_crm(row) if row else {}

    def list_callback_tasks(
        self, customer_id: str = "", status: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if customer_id:
            clauses.append("customer_id=?")
            params.append(customer_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " AND ".join(clauses) if clauses else "1=1"
        params.append(limit)
        rows = self.fetchall(
            f"SELECT * FROM callback_tasks WHERE {where} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        )
        return [self._decode_crm(row) for row in rows]

    def create_verification(
        self,
        *,
        call_id: str,
        customer_id: str,
        phone: str,
        method: str,
        matches: dict[str, Any],
        ttl_minutes: int = 30,
    ) -> dict[str, Any]:
        verification_id = str(uuid.uuid4())
        verified_at = _now_dt()
        expires_at = verified_at + timedelta(minutes=max(1, ttl_minutes))
        self.execute(
            """
            INSERT INTO verification_sessions
            (id,call_id,customer_id,phone,method,matches_json,verified_at,expires_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                verification_id,
                call_id,
                customer_id,
                phone,
                method,
                _json(matches),
                verified_at.isoformat(),
                expires_at.isoformat(),
            ),
        )
        return {
            "id": verification_id,
            "call_id": call_id,
            "customer_id": customer_id,
            "method": method,
            "matches": matches,
            "verified_at": verified_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

    def is_verified(self, call_id: str, customer_id: str) -> bool:
        row = self.fetchone(
            """
            SELECT id FROM verification_sessions
            WHERE call_id=? AND customer_id=? AND expires_at>?
            ORDER BY verified_at DESC LIMIT 1
            """,
            (call_id, customer_id, _now()),
        )
        return bool(row)

    def record_upload(self, value: dict[str, Any]) -> dict[str, Any]:
        upload_id = str(value.get("id") or uuid.uuid4())
        self.execute(
            """
            INSERT INTO uploads
            (id,token_id,customer_id,call_id,filename,stored_path,content_type,size_bytes,metadata_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                upload_id,
                str(value.get("token_id") or ""),
                str(value.get("customer_id") or ""),
                str(value.get("call_id") or ""),
                str(value.get("filename") or "upload.bin"),
                str(value.get("stored_path") or ""),
                str(value.get("content_type") or "application/octet-stream"),
                int(value.get("size_bytes") or 0),
                _json(value.get("metadata") or {}),
                _now(),
            ),
        )
        row = self.fetchone("SELECT * FROM uploads WHERE id=?", (upload_id,))
        return self._decode_crm(row) if row else {}

    def list_uploads(self, customer_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if customer_id:
            rows = self.fetchall(
                "SELECT * FROM uploads WHERE customer_id=? ORDER BY created_at DESC LIMIT ?",
                (customer_id, limit),
            )
        else:
            rows = self.fetchall("SELECT * FROM uploads ORDER BY created_at DESC LIMIT ?", (limit,))
        return [self._decode_crm(row) for row in rows]

    @staticmethod
    def _decode_crm(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        if "metadata_json" in result:
            result["metadata"] = _decode_json(result.pop("metadata_json", "{}"), {})
        return result

    def dashboard_counts(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        queries = {
            "gate_sessions": "SELECT COUNT(*) AS value FROM gate_sessions",
            "call_intakes": "SELECT COUNT(*) AS value FROM call_intakes",
            "partial_intakes": "SELECT COUNT(*) AS value FROM call_intakes WHERE status LIKE 'partial_%'",
            "outbound_pending": "SELECT COUNT(*) AS value FROM outbound_jobs WHERE status IN ('pending','retry')",
            "outbound_dialing": "SELECT COUNT(*) AS value FROM outbound_jobs WHERE status IN ('dialing','answered')",
            "outbound_completed": "SELECT COUNT(*) AS value FROM outbound_jobs WHERE status='completed'",
            "suppressed_numbers": "SELECT COUNT(*) AS value FROM suppressions",
            "outbox_pending": "SELECT COUNT(*) AS value FROM integration_outbox WHERE status IN ('pending','retry')",
            "customers": "SELECT COUNT(*) AS value FROM customers",
            "open_leads": "SELECT COUNT(*) AS value FROM leads WHERE status NOT IN ('closed','won','lost')",
            "upcoming_appointments": "SELECT COUNT(*) AS value FROM appointments WHERE status IN ('scheduled','confirmed') AND start_at>=?",
            "open_invoices": "SELECT COUNT(*) AS value FROM invoices WHERE status NOT IN ('paid','void','canceled') AND amount_due>0",
            "open_callbacks": "SELECT COUNT(*) AS value FROM callback_tasks WHERE status='open'",
        }
        for key, sql in queries.items():
            row = self.fetchone(sql, (_now(),)) if key == "upcoming_appointments" else self.fetchone(sql)
            result[key] = int(row["value"] if row else 0)
        return result

    # ── Recording CRUD ────────────────────────────────────────────────────────

    def create_recording(self, req: "Any") -> dict[str, Any]:
        """Insert a new recording row when MixMonitor starts."""
        rec_id = str(uuid.uuid4())
        now = _now()
        import datetime as _dt
        expires_at = (_now_dt() + _dt.timedelta(days=90)).isoformat()
        self.execute(
            """
            INSERT INTO recordings
            (id,asterisk_unique_id,call_id,direction,caller_number,called_number,
             agent,campaign_id,source,status,disclosure_played,disclosure_skipped_reason,
             started_at,retention_expires_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                rec_id,
                str(req.asterisk_unique_id),
                str(req.call_id),
                str(req.direction.value if hasattr(req.direction, "value") else req.direction),
                str(req.caller_number),
                str(req.called_number),
                str(req.agent),
                str(req.campaign_id),
                str(req.source.value if hasattr(req.source, "value") else req.source),
                "recording",
                int(bool(req.disclosure_played)),
                str(req.disclosure_skipped_reason),
                now,
                expires_at,
                now,
                now,
            ),
        )
        row = self.fetchone("SELECT * FROM recordings WHERE id=?", (rec_id,))
        return dict(row) if row else {}

    def finalize_recording(
        self,
        asterisk_unique_id: str,
        *,
        file_path: str = "",
        file_size: int = 0,
        mime_type: str = "audio/wav",
        sha256: str = "",
        duration_seconds: float = 0.0,
        status: str = "completed",
        protected_segment: bool = False,
        error: str = "",
        retention_days: int = 90,
    ) -> "dict[str, Any] | None":
        row = self.fetchone(
            "SELECT id FROM recordings WHERE asterisk_unique_id=? AND status='recording'",
            (asterisk_unique_id,),
        )
        if not row:
            return None
        rec_id = row["id"]
        now = _now()
        import datetime as _dt
        expires_at = (_now_dt() + _dt.timedelta(days=max(1, retention_days))).isoformat()
        final_status = "failed" if error else status
        self.execute(
            """
            UPDATE recordings SET
              status=?, file_path=?, file_size=?, mime_type=?, sha256=?,
              duration_seconds=?, ended_at=?, retention_expires_at=?,
              protected_segment=?, updated_at=?
            WHERE id=?
            """,
            (
                final_status, file_path, file_size, mime_type, sha256,
                duration_seconds, now, expires_at, int(protected_segment), now, rec_id,
            ),
        )
        result = self.fetchone("SELECT * FROM recordings WHERE id=?", (rec_id,))
        return dict(result) if result else None

    def get_recording(self, recording_id: str) -> "dict[str, Any] | None":
        row = self.fetchone("SELECT * FROM recordings WHERE id=?", (recording_id,))
        return dict(row) if row else None

    def get_recording_by_asterisk_id(self, asterisk_unique_id: str) -> "dict[str, Any] | None":
        row = self.fetchone(
            "SELECT * FROM recordings WHERE asterisk_unique_id=? ORDER BY created_at DESC LIMIT 1",
            (asterisk_unique_id,),
        )
        return dict(row) if row else None

    def list_recordings(
        self,
        *,
        direction: "str | None" = None,
        source: "str | None" = None,
        agent: "str | None" = None,
        campaign_id: "str | None" = None,
        status: "str | None" = None,
        caller_number: "str | None" = None,
        call_id: "str | None" = None,
        date_from: "str | None" = None,
        date_to: "str | None" = None,
        limit: int = 50,
        offset: int = 0,
    ) -> "list[dict[str, Any]]":
        clauses: list[str] = []
        params: list[Any] = []
        if direction:
            clauses.append("direction=?"); params.append(direction)
        if source:
            clauses.append("source=?"); params.append(source)
        if agent:
            clauses.append("agent=?"); params.append(agent)
        if campaign_id:
            clauses.append("campaign_id=?"); params.append(campaign_id)
        if status:
            clauses.append("status=?"); params.append(status)
        if caller_number:
            clauses.append("caller_number=?"); params.append(caller_number)
        if call_id:
            clauses.append("call_id=?"); params.append(call_id)
        if date_from:
            clauses.append("started_at>=?"); params.append(date_from)
        if date_to:
            clauses.append("started_at<=?"); params.append(date_to)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.fetchall(
            f"SELECT * FROM recordings {where} ORDER BY started_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [dict(r) for r in rows]

    def set_recording_hold(
        self,
        recording_id: str,
        *,
        held: bool,
        hold_reason: str = "",
    ) -> "dict[str, Any] | None":
        now = _now()
        new_status = "held" if held else "completed"
        self.execute(
            "UPDATE recordings SET is_held=?, hold_reason=?, status=?, updated_at=? WHERE id=?",
            (int(held), hold_reason, new_status, now, recording_id),
        )
        row = self.fetchone("SELECT * FROM recordings WHERE id=?", (recording_id,))
        return dict(row) if row else None

    def delete_recording_file(self, recording_id: str) -> "dict[str, Any] | None":
        """Mark recording deleted; metadata row is preserved."""
        now = _now()
        self.execute(
            "UPDATE recordings SET status='deleted', file_path='', updated_at=? WHERE id=?",
            (now, recording_id),
        )
        row = self.fetchone("SELECT * FROM recordings WHERE id=?", (recording_id,))
        return dict(row) if row else None

    def expire_old_recordings(
        self,
        *,
        dry_run: bool = False,
        before_iso: "str | None" = None,
    ) -> "list[dict[str, Any]]":
        """Return expired recordings (held ones are skipped). Marks them expired unless dry_run."""
        cutoff = before_iso or _now()
        rows = self.fetchall(
            """SELECT * FROM recordings
               WHERE retention_expires_at < ?
                 AND is_held = 0
                 AND status NOT IN ('deleted','expired','failed')
               ORDER BY retention_expires_at ASC""",
            (cutoff,),
        )
        expired = [dict(r) for r in rows]
        if not dry_run:
            now = _now()
            for rec in expired:
                self.execute(
                    "UPDATE recordings SET status='expired', file_path='', updated_at=? WHERE id=?",
                    (now, rec["id"]),
                )
        return expired

    def mark_recording_roomflow_queued(self, recording_id: str) -> None:
        self.execute(
            "UPDATE recordings SET roomflow_queued=1, updated_at=? WHERE id=?",
            (_now(), recording_id),
        )
