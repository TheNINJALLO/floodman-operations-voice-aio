from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
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
                    recipient TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response TEXT NOT NULL DEFAULT '',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at DESC);
                CREATE INDEX IF NOT EXISTS idx_messages_call ON messages(call_id, id);
                """
            )

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

    def record_notification(self, call_id: int, kind: str, recipient: str, status: str, response: str, key: str) -> bool:
        try:
            with self.connect() as db:
                db.execute(
                    "INSERT INTO notifications(call_id,kind,recipient,status,response,idempotency_key,created_at) VALUES(?,?,?,?,?,?,?)",
                    (call_id, kind, recipient, status, response[:2000], key, utcnow()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

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
            notifications = [dict(value) for value in db.execute("SELECT kind,recipient,status,response,created_at FROM notifications WHERE call_id=? ORDER BY id", (call_id,))]
        item = dict(row)
        item["snapshot"] = json.loads(item.pop("snapshot_json") or "{}")
        item["messages"] = messages
        item["notifications"] = notifications
        return item

    def delete_call_by_uuid(self, call_uuid: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM calls WHERE call_uuid=?", (call_uuid,))
