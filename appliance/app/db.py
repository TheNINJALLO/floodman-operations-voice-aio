from __future__ import annotations

import json
import hashlib
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.models import IntakeState


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_uuid TEXT NOT NULL UNIQUE,
                    caller_number TEXT NOT NULL DEFAULT '',
                    called_number TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    outcome TEXT NOT NULL DEFAULT '',
                    current_stage TEXT NOT NULL DEFAULT 'issue',
                    last_prompt TEXT NOT NULL DEFAULT '',
                    transcript_text TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS intakes (
                    call_id INTEGER PRIMARY KEY REFERENCES calls(id) ON DELETE CASCADE,
                    snapshot_json TEXT NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    notification_status TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'sms',
                    recipient TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    display_name TEXT NOT NULL,
                    email TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'viewer' CHECK(role IN ('admin','manager','viewer')),
                    active INTEGER NOT NULL DEFAULT 1,
                    notify_new_calls INTEGER NOT NULL DEFAULT 1,
                    notify_completed_calls INTEGER NOT NULL DEFAULT 1,
                    notify_emergencies INTEGER NOT NULL DEFAULT 1,
                    email_notifications INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT,
                    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
                );
                CREATE TABLE IF NOT EXISTS user_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    is_recovery INTEGER NOT NULL DEFAULT 0,
                    csrf_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    event_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '/',
                    call_id INTEGER REFERENCES calls(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    read_at TEXT,
                    UNIQUE(user_id, event_key)
                );
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    user_agent TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    last_success_at TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                    actor_name TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS business_event_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','sending','sent')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    UNIQUE(call_id, event_sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_call ON messages(call_id, id);
                CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON user_sessions(expires_at);
                CREATE INDEX IF NOT EXISTS idx_user_notifications ON user_notifications(user_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_business_outbox_due
                    ON business_event_outbox(status, next_attempt_at, id);
                """
            )
            self._ensure_column(db, "users", "email", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(db, "users", "email_notifications", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(db, "notifications", "channel", "TEXT NOT NULL DEFAULT 'sms'")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_unique ON users(email COLLATE NOCASE) WHERE email<>''")
            db.execute("CREATE INDEX IF NOT EXISTS idx_notifications_channel ON notifications(channel, status)")
            # A process can stop after claiming an item but before recording its
            # result. Return those durable local records to the retry queue.
            db.execute("UPDATE business_event_outbox SET status='pending' WHERE status='sending'")

    @staticmethod
    def _ensure_column(db: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def create_call(self, state: IntakeState) -> int:
        with self._lock, self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO calls(call_uuid,caller_number,called_number,started_at,current_stage) VALUES(?,?,?,?,?)",
                (state.call_uuid, state.caller_number, state.called_number, utcnow(), state.stage),
            )
            row = db.execute("SELECT id FROM calls WHERE call_uuid=?", (state.call_uuid,)).fetchone()
            assert row
            call_id = int(row["id"])
            self._save_intake_db(db, call_id, state)
            return call_id

    def call_id(self, call_uuid: str) -> int | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM calls WHERE call_uuid=?", (call_uuid,)).fetchone()
            return int(row["id"]) if row else None

    def add_message(self, call_id: int, role: str, text: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        with self._lock, self.connect() as db:
            db.execute("INSERT INTO messages(call_id,role,text,created_at) VALUES(?,?,?,?)", (call_id, role, text, utcnow()))
            transcript = "\n".join(f"{row['role']}: {row['text']}" for row in db.execute("SELECT role,text FROM messages WHERE call_id=? ORDER BY id", (call_id,)))
            db.execute("UPDATE calls SET transcript_text=? WHERE id=?", (transcript, call_id))

    def save_intake(self, call_id: int, state: IntakeState, notification_status: str = "") -> None:
        with self._lock, self.connect() as db:
            self._save_intake_db(db, call_id, state, notification_status)
            db.execute("UPDATE calls SET current_stage=? WHERE id=?", (state.stage, call_id))

    def _save_intake_db(self, db: sqlite3.Connection, call_id: int, state: IntakeState, notification_status: str = "") -> None:
        db.execute(
            """INSERT INTO intakes(call_id,snapshot_json,completed,notification_status,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(call_id) DO UPDATE SET snapshot_json=excluded.snapshot_json,
               completed=excluded.completed,
               notification_status=CASE WHEN excluded.notification_status='' THEN intakes.notification_status ELSE excluded.notification_status END,
               updated_at=excluded.updated_at""",
            (call_id, json.dumps(state.to_dict(), ensure_ascii=False), int(state.completed), notification_status, utcnow()),
        )

    def update_prompt(self, call_id: int, text: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE calls SET last_prompt=? WHERE id=?", (text, call_id))

    def finish_call(self, call_id: int, outcome: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE calls SET ended_at=?, status='completed', outcome=? WHERE id=?", (utcnow(), outcome, call_id))

    # Business Suite event outbox ------------------------------------
    def enqueue_business_event(
        self,
        call_id: int,
        event_type: str,
        payload_factory: Any,
    ) -> dict[str, Any]:
        """Commit the next ordered event locally before any network attempt."""
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COALESCE(MAX(event_sequence), -1) + 1 AS sequence FROM business_event_outbox WHERE call_id=?",
                (call_id,),
            ).fetchone()
            sequence = int(row["sequence"])
            payload = payload_factory(sequence)
            event_id = str(payload["event_id"])
            db.execute(
                """INSERT INTO business_event_outbox(
                       call_id,event_id,event_type,event_sequence,payload_json,status,
                       attempts,next_attempt_at,last_error,created_at
                   ) VALUES(?,?,?,?,?,'pending',0,0,'',?)""",
                (
                    call_id,
                    event_id,
                    event_type,
                    sequence,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    utcnow(),
                ),
            )
        return payload

    def claim_business_event(self, now: float | None = None) -> dict[str, Any] | None:
        due = time.time() if now is None else now
        with self._lock, self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                """SELECT * FROM business_event_outbox
                   WHERE status='pending' AND next_attempt_at<=?
                   ORDER BY id LIMIT 1""",
                (due,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE business_event_outbox SET status='sending',attempts=attempts+1 WHERE id=?",
                (row["id"],),
            )
            item = dict(row)
            item["attempts"] = int(item["attempts"]) + 1
            item["payload"] = json.loads(item.pop("payload_json"))
            return item

    def complete_business_event(self, outbox_id: int) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE business_event_outbox SET status='sent',last_error='',sent_at=? WHERE id=?",
                (utcnow(), outbox_id),
            )

    def retry_business_event(self, outbox_id: int, error: str, delay_seconds: float) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE business_event_outbox
                   SET status='pending',last_error=?,next_attempt_at=? WHERE id=?""",
                (str(error or "delivery failed")[:1000], time.time() + max(0.0, delay_seconds), outbox_id),
            )

    def business_event_counts(self) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT status,COUNT(*) AS count FROM business_event_outbox GROUP BY status"
            ).fetchall()
        counts = {"pending": 0, "sending": 0, "sent": 0}
        counts.update({str(row["status"]): int(row["count"]) for row in rows})
        return counts

    def record_notification(
        self,
        call_id: int,
        kind: str,
        recipient: str,
        status: str,
        response: str,
        key: str,
        *,
        channel: str = "sms",
    ) -> bool:
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO notifications(call_id,kind,channel,recipient,status,response,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (call_id, kind, channel, recipient, status, response[:2000], key, utcnow()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def reserve_notification(self, call_id: int, kind: str, channel: str, recipient: str, key: str) -> int | None:
        try:
            with self.connect() as db:
                result = db.execute(
                    """INSERT INTO notifications(call_id,kind,channel,recipient,status,response,idempotency_key,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (call_id, kind, channel, recipient, "queued", "Waiting for delivery", key, utcnow()),
                )
            return int(result.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def complete_notification(self, notification_id: int, status: str, response: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE notifications SET status=?,response=? WHERE id=?",
                (str(status or "failed")[:40], str(response or "")[:2000], notification_id),
            )

    def notification_exists(self, key: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM notifications WHERE idempotency_key=?", (key,)).fetchone() is not None

    def list_calls(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT c.*, i.snapshot_json, i.completed AS intake_completed, i.notification_status
                   FROM calls c LEFT JOIN intakes i ON i.call_id=c.id ORDER BY c.id DESC LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
            output.append(item)
        return output

    def get_call(self, call_id: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT c.*, i.snapshot_json, i.completed AS intake_completed, i.notification_status
                   FROM calls c LEFT JOIN intakes i ON i.call_id=c.id WHERE c.id=?""",
                (call_id,),
            ).fetchone()
            if not row:
                return None
            messages = [dict(value) for value in db.execute("SELECT role,text,created_at FROM messages WHERE call_id=? ORDER BY id", (call_id,))]
            notifications = [
                dict(value)
                for value in db.execute(
                    "SELECT channel,kind,recipient,status,response,created_at FROM notifications WHERE call_id=? ORDER BY id",
                    (call_id,),
                )
            ]
        item = dict(row)
        item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
        item["messages"] = messages
        item["notifications"] = notifications
        return item

    def delete_call_by_uuid(self, call_uuid: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM calls WHERE call_uuid=?", (call_uuid,))

    def delete_call(self, call_id: int) -> bool:
        with self.connect() as db:
            result = db.execute("DELETE FROM calls WHERE id=?", (call_id,))
            return result.rowcount > 0

    def update_call(self, call_id: int, values: dict[str, Any]) -> None:
        allowed_call = {"status", "outcome", "current_stage"}
        allowed_snapshot = {
            "name",
            "email",
            "email_status",
            "phone",
            "address",
            "description",
            "service_key",
            "service_status",
            "service_area_status",
            "service_area_city",
            "property_context",
            "affected_area",
            "timing_summary",
            "source_summary",
            "safety_summary",
            "urgency",
            "department",
            "completed",
        }
        with self._lock, self.connect() as db:
            row = db.execute(
                "SELECT c.id,i.snapshot_json FROM calls c LEFT JOIN intakes i ON i.call_id=c.id WHERE c.id=?",
                (call_id,),
            ).fetchone()
            if not row:
                raise LookupError("Call not found.")
            call_updates = {key: str(values[key] or "")[:200] for key in allowed_call if key in values}
            if call_updates:
                assignments = ",".join(f"{key}=?" for key in call_updates)
                db.execute(
                    f"UPDATE calls SET {assignments} WHERE id=?",
                    (*call_updates.values(), call_id),
                )
            snapshot = json.loads(row["snapshot_json"] or "{}")
            for key in allowed_snapshot:
                if key not in values:
                    continue
                if key == "completed":
                    snapshot[key] = bool(values[key])
                else:
                    snapshot[key] = str(values[key] or "").strip()[:4000]
            db.execute(
                "UPDATE intakes SET snapshot_json=?,completed=?,updated_at=? WHERE call_id=?",
                (json.dumps(snapshot, ensure_ascii=False), int(bool(snapshot.get("completed"))), utcnow(), call_id),
            )

    def dashboard_counts(self) -> dict[str, int]:
        with self.connect() as db:
            total = int(db.execute("SELECT COUNT(*) FROM calls").fetchone()[0])
            active = int(db.execute("SELECT COUNT(*) FROM calls WHERE status='active'").fetchone()[0])
            emergencies = int(
                db.execute("SELECT COUNT(*) FROM intakes WHERE json_extract(snapshot_json,'$.urgency')='emergency'").fetchone()[0]
            )
            completed = int(db.execute("SELECT COUNT(*) FROM intakes WHERE completed=1").fetchone()[0])
        return {"total": total, "active": active, "emergencies": emergencies, "completed": completed}

    # User accounts and sessions -------------------------------------
    def user_count(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(
        self,
        username: str,
        display_name: str,
        password_hash: str,
        role: str,
        preferences: dict[str, bool],
        created_by: int | None,
        email: str = "",
    ) -> int:
        now = utcnow()
        with self.connect() as db:
            result = db.execute(
                """INSERT INTO users(
                       username,display_name,email,password_hash,role,active,
                       notify_new_calls,notify_completed_calls,notify_emergencies,email_notifications,
                       created_at,updated_at,created_by
                   ) VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?)""",
                (
                    username,
                    display_name,
                    email,
                    password_hash,
                    role,
                    int(bool(preferences.get("notify_new_calls", True))),
                    int(bool(preferences.get("notify_completed_calls", True))),
                    int(bool(preferences.get("notify_emergencies", True))),
                    int(bool(preferences.get("email_notifications", False))),
                    now,
                    now,
                    created_by,
                ),
            )
            return int(result.lastrowid)

    def list_users(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT u.id,u.username,u.display_name,u.email,u.role,u.active,
                          u.notify_new_calls,u.notify_completed_calls,u.notify_emergencies,u.email_notifications,
                          u.created_at,u.updated_at,u.last_login_at,
                          COUNT(p.id) AS push_devices
                   FROM users u LEFT JOIN push_subscriptions p ON p.user_id=u.id
                   GROUP BY u.id ORDER BY u.active DESC,u.display_name COLLATE NOCASE"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_user(self, user_id: int, *, include_secret: bool = False) -> dict[str, Any] | None:
        columns = "*" if include_secret else "id,username,display_name,email,role,active,notify_new_calls,notify_completed_calls,notify_emergencies,email_notifications,created_at,updated_at,last_login_at"
        with self.connect() as db:
            row = db.execute(f"SELECT {columns} FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username: str, *, include_secret: bool = False) -> dict[str, Any] | None:
        columns = "*" if include_secret else "id,username,display_name,email,role,active,notify_new_calls,notify_completed_calls,notify_emergencies,email_notifications,created_at,updated_at,last_login_at"
        with self.connect() as db:
            row = db.execute(f"SELECT {columns} FROM users WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        return dict(row) if row else None

    def update_user(
        self,
        user_id: int,
        username: str,
        display_name: str,
        role: str,
        active: bool,
        preferences: dict[str, bool],
        email: str = "",
    ) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE users SET username=?,display_name=?,email=?,role=?,active=?,
                          notify_new_calls=?,notify_completed_calls=?,notify_emergencies=?,email_notifications=?,updated_at=?
                   WHERE id=?""",
                (
                    username,
                    display_name,
                    email,
                    role,
                    int(active),
                    int(bool(preferences.get("notify_new_calls"))),
                    int(bool(preferences.get("notify_completed_calls"))),
                    int(bool(preferences.get("notify_emergencies"))),
                    int(bool(preferences.get("email_notifications"))),
                    utcnow(),
                    user_id,
                ),
            )
            if not active:
                db.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))

    def update_user_password(self, user_id: int, password_hash: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE users SET password_hash=?,updated_at=? WHERE id=?", (password_hash, utcnow(), user_id))
            db.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))

    def delete_user(self, user_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM users WHERE id=?", (user_id,))

    def active_admin_count(self) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0])

    def record_user_login(self, user_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE users SET last_login_at=? WHERE id=?", (utcnow(), user_id))

    def create_session(self, user_id: int | None, is_recovery: bool, hours: int) -> tuple[str, str]:
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(hours=hours)
        with self.connect() as db:
            db.execute("DELETE FROM user_sessions WHERE expires_at<=?", (now.isoformat(),))
            db.execute(
                "INSERT INTO user_sessions(token_hash,user_id,is_recovery,csrf_token,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                (hashlib.sha256(token.encode("utf-8")).hexdigest(), user_id, int(is_recovery), csrf, now.isoformat(), expires.isoformat()),
            )
        return token, csrf

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        now = utcnow()
        with self.connect() as db:
            row = db.execute(
                """SELECT s.user_id,s.is_recovery,s.csrf_token,s.expires_at,
                          u.username,u.display_name,u.role,u.active
                   FROM user_sessions s LEFT JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.expires_at>?
                     AND (s.is_recovery=1 OR u.active=1)""",
                (token_hash, now),
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM user_sessions WHERE token_hash=?", (token_hash,))

    # In-app and browser notifications -------------------------------
    def create_user_notifications(
        self,
        *,
        event_key: str,
        kind: str,
        title: str,
        body: str,
        url: str,
        call_id: int | None = None,
        target_user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        preference = {
            "call_started": "notify_new_calls",
            "completed_intake": "notify_completed_calls",
            "emergency": "notify_emergencies",
        }.get(kind)
        created: list[dict[str, Any]] = []
        with self.connect() as db:
            query = "SELECT id FROM users WHERE active=1"
            if preference:
                query += f" AND {preference}=1"
            parameters: tuple[Any, ...] = ()
            if target_user_id is not None:
                query += " AND id=?"
                parameters = (target_user_id,)
            for row in db.execute(query, parameters).fetchall():
                try:
                    result = db.execute(
                        """INSERT INTO user_notifications(user_id,event_key,kind,title,body,url,call_id,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (row["id"], event_key, kind, title[:140], body[:500], url[:500], call_id, utcnow()),
                    )
                except sqlite3.IntegrityError:
                    continue
                created.append({"id": int(result.lastrowid), "user_id": int(row["id"]), "title": title, "body": body, "url": url, "kind": kind})
        return created

    def list_email_notification_recipients(
        self,
        kind: str,
        *,
        target_user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        preference = {
            "call_started": "notify_new_calls",
            "completed_intake": "notify_completed_calls",
            "emergency": "notify_emergencies",
        }.get(kind)
        query = (
            "SELECT id,email,display_name FROM users "
            "WHERE active=1 AND email_notifications=1 AND email<>'' "
            "AND role IN ('admin','manager','viewer')"
        )
        parameters: list[Any] = []
        if preference:
            query += f" AND {preference}=1"
        if target_user_id is not None:
            query += " AND id=?"
            parameters.append(target_user_id)
        query += " ORDER BY id"
        with self.connect() as db:
            rows = db.execute(query, tuple(parameters)).fetchall()
        return [dict(row) for row in rows]

    def list_user_notifications(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM user_notifications WHERE user_id=? ORDER BY id DESC LIMIT ?",
                (user_id, max(1, min(limit, 250))),
            ).fetchall()
        return [dict(row) for row in rows]

    def unread_notification_count(self, user_id: int) -> int:
        with self.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM user_notifications WHERE user_id=? AND read_at IS NULL", (user_id,)).fetchone()[0])

    def mark_notifications_read(self, user_id: int, notification_id: int | None = None) -> None:
        with self.connect() as db:
            if notification_id is None:
                db.execute("UPDATE user_notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (utcnow(), user_id))
            else:
                db.execute("UPDATE user_notifications SET read_at=? WHERE user_id=? AND id=?", (utcnow(), user_id, notification_id))

    def delete_user_notification(self, user_id: int, notification_id: int) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM user_notifications WHERE user_id=? AND id=?", (user_id, notification_id))

    def upsert_push_subscription(self, user_id: int, endpoint: str, p256dh: str, auth: str, user_agent: str) -> None:
        with self.connect() as db:
            db.execute(
                """INSERT INTO push_subscriptions(user_id,endpoint,p256dh,auth,user_agent,created_at,failure_count)
                   VALUES(?,?,?,?,?,?,0)
                   ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id,p256dh=excluded.p256dh,
                     auth=excluded.auth,user_agent=excluded.user_agent,failure_count=0""",
                (user_id, endpoint, p256dh, auth, user_agent[:500], utcnow()),
            )

    def list_push_subscriptions(self, user_id: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,user_id,endpoint,p256dh,auth,user_agent,created_at,last_success_at,failure_count FROM push_subscriptions WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_push_subscription(self, user_id: int, endpoint: str = "", subscription_id: int | None = None) -> None:
        with self.connect() as db:
            if subscription_id is not None:
                db.execute("DELETE FROM push_subscriptions WHERE user_id=? AND id=?", (user_id, subscription_id))
            elif endpoint:
                db.execute("DELETE FROM push_subscriptions WHERE user_id=? AND endpoint=?", (user_id, endpoint))

    def push_succeeded(self, subscription_id: int) -> None:
        with self.connect() as db:
            db.execute("UPDATE push_subscriptions SET last_success_at=?,failure_count=0 WHERE id=?", (utcnow(), subscription_id))

    def push_failed(self, subscription_id: int, *, remove: bool = False) -> None:
        with self.connect() as db:
            if remove:
                db.execute("DELETE FROM push_subscriptions WHERE id=?", (subscription_id,))
            else:
                db.execute("UPDATE push_subscriptions SET failure_count=failure_count+1 WHERE id=?", (subscription_id,))

    # Audit trail -----------------------------------------------------
    def audit(self, user_id: int | None, actor_name: str, action: str, entity_type: str, entity_id: str = "", detail: dict[str, Any] | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO audit_events(user_id,actor_name,action,entity_type,entity_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, actor_name[:80], action[:100], entity_type[:80], str(entity_id)[:120], json.dumps(detail or {}, ensure_ascii=False)[:4000], utcnow()),
            )

    def list_audit_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json") or "{}")
            output.append(item)
        return output
