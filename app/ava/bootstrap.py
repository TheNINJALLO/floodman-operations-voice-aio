from __future__ import annotations

import argparse
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.ava.agents import AGENTS
from app.config import Settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    extension TEXT,
    role_label TEXT,
    provider TEXT NOT NULL,
    voice TEXT,
    greeting TEXT,
    prompt TEXT NOT NULL,
    tools_json TEXT,
    tool_configs_json TEXT,
    mcp_json TEXT,
    audio_profile TEXT,
    extra_json TEXT,
    is_operator_managed INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    source_file TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    notes TEXT,
    email_recipient TEXT,
    email_from TEXT,
    email_enabled INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agents_slug ON agents(slug);
CREATE INDEX IF NOT EXISTS idx_agents_mgmt ON agents(is_operator_managed);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_default ON agents(is_default) WHERE is_default = 1;
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    contexts_hash TEXT
);
"""

MARKER = "Managed by Floodman Operations Voice AIO"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def provision_agents(settings: Settings) -> list[str]:
    path = Path(settings.agents_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    created_or_updated: list[str] = []
    try:
        for definition in AGENTS:
            row = conn.execute("SELECT * FROM agents WHERE slug=?", (definition.slug,)).fetchone()
            tools_json = json.dumps(list(definition.tools))
            extra_json = json.dumps(
                {
                    "pipeline": settings.ava_pipeline,
                    "connection_audio": "tone:ring",
                    "floodman_managed": True,
                },
                sort_keys=True,
            )
            tool_configs_json = json.dumps({}, sort_keys=True)
            now = _now()
            if row:
                notes = str(row["notes"] or "")
                if not settings.reconcile_ava_agents or (MARKER not in notes and notes.strip()):
                    continue
                conn.execute(
                    """
                    UPDATE agents SET display_name=?,role_label=?,provider=?,greeting=?,prompt=?,
                        tools_json=?,tool_configs_json=?,audio_profile=?,extra_json=?,is_active=1,
                        updated_at=?,notes=? WHERE slug=?
                    """,
                    (
                        definition.display_name,
                        definition.role_label,
                        settings.ava_provider,
                        definition.greeting,
                        definition.prompt,
                        tools_json,
                        tool_configs_json,
                        settings.ava_audio_profile,
                        extra_json,
                        now,
                        MARKER,
                        definition.slug,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO agents (
                        id,slug,display_name,extension,role_label,provider,voice,greeting,prompt,
                        tools_json,tool_configs_json,mcp_json,audio_profile,extra_json,
                        is_operator_managed,is_active,is_default,source_file,created_at,updated_at,notes,
                        email_recipient,email_from,email_enabled
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,0,?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid.uuid4()),
                        definition.slug,
                        definition.display_name,
                        None,
                        definition.role_label,
                        settings.ava_provider,
                        None,
                        definition.greeting,
                        definition.prompt,
                        tools_json,
                        tool_configs_json,
                        None,
                        settings.ava_audio_profile,
                        extra_json,
                        None,
                        now,
                        now,
                        MARKER,
                        None,
                        None,
                        None,
                    ),
                )
            created_or_updated.append(definition.slug)

        default = conn.execute("SELECT slug FROM agents WHERE is_default=1 AND is_active=1").fetchone()
        if not default or str(default["slug"]).startswith("floodman_"):
            conn.execute("UPDATE agents SET is_default=0 WHERE is_default=1")
            conn.execute(
                "UPDATE agents SET is_default=1 WHERE slug='floodman_inbound' AND is_active=1"
            )
        conn.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return created_or_updated
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Provision Floodman agents in AVA agents.db")
    parser.add_argument("--project-root", default=None)
    args = parser.parse_args()
    settings = Settings.from_env(Path(args.project_root) if args.project_root else None)
    slugs = provision_agents(settings)
    print(json.dumps({"agents_db": str(settings.agents_db_path), "provisioned": slugs}, indent=2))


if __name__ == "__main__":
    main()
