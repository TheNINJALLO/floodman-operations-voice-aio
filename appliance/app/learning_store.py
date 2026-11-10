"""Private learning records, independent of the live call database and provider."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class LearningStore:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        self.path = root / "learning.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS state(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS contacts(id TEXT PRIMARY KEY, project_id TEXT NOT NULL, seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(
                    id TEXT PRIMARY KEY, origin TEXT NOT NULL, project_id TEXT NOT NULL,
                    external_id TEXT NOT NULL, digest TEXT NOT NULL, split TEXT NOT NULL,
                    historical INTEGER NOT NULL, received REAL NOT NULL, updated REAL NOT NULL,
                    payload TEXT NOT NULL, observations TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, due REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', analysis TEXT NOT NULL DEFAULT '{}',
                    review TEXT NOT NULL DEFAULT 'pending', knowledge_slug TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS sources_queue ON sources(status,historical,due);
            """)
            db.execute("UPDATE sources SET status='queued',error='restart' WHERE status='analyzing'")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def get(self, ident, default=None):
        with self.connect() as db: row = db.execute("SELECT data FROM state WHERE id=?", (ident,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, ident, value):
        with self.connect() as db: db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (ident, json.dumps(value)))

    def contact(self, project, external):
        with self.connect() as db:
            db.execute("INSERT INTO contacts VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET seen=excluded.seen", (digest([project, external]), project, time.time()))

    def save_source(self, origin, project, external, payload, observations, historical):
        ident = digest([origin, project, external])
        fingerprint = digest(payload)
        at = time.time()
        with self.connect() as db:
            old = db.execute("SELECT * FROM sources WHERE id=?", (ident,)).fetchone()
            if old and old['digest'] == fingerprint: return False
            if old:
                previous = json.loads(old['payload'])
                if previous.get('lead_reference') and previous.get('lead_reference') != payload.get('lead_reference'):
                    raise ValueError('Source conversation ownership changed; review required.')
                db.execute("UPDATE sources SET digest=?,updated=?,payload=?,observations=?,status=?,attempts=0,due=0,error='',analysis='{}',review='pending' WHERE id=?",
                    (fingerprint, at, json.dumps(payload), json.dumps(observations), 'queued' if payload['messages'] else 'empty', ident))
            else:
                split = 'holdout' if int(ident[:8], 16) % 5 == 0 else 'examples'
                db.execute("INSERT INTO sources(id,origin,project_id,external_id,digest,split,historical,received,updated,payload,observations,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ident, origin, project, external, fingerprint, split, int(historical), at, at, json.dumps(payload), json.dumps(observations), 'queued' if payload['messages'] else 'empty'))
        return True

    @staticmethod
    def decode(row):
        if not row: return None
        result = dict(row)
        for k in ('payload', 'observations', 'analysis'): result[k] = json.loads(result[k])
        return result

    def source(self, ident):
        with self.connect() as db: return self.decode(db.execute("SELECT * FROM sources WHERE id=?", (ident,)).fetchone())

    def next(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            history_pending = bool(db.execute("SELECT 1 FROM sources WHERE historical=1 AND status IN ('queued','retry','analyzing') LIMIT 1").fetchone())
            row = db.execute("SELECT * FROM sources WHERE status IN ('queued','retry') AND due<=? AND (historical=1 OR ?=0) ORDER BY historical DESC,received,id LIMIT 1", (time.time(), int(history_pending))).fetchone()
            if row: db.execute("UPDATE sources SET status='analyzing' WHERE id=?", (row['id'],))
            return self.decode(row)

    def complete(self, row, analysis):
        with self.connect() as db:
            db.execute("UPDATE sources SET status='analyzed',analysis=?,error='' WHERE id=? AND digest=?", (json.dumps(analysis), row['id'], row['digest']))

    def defer(self, row, code, *, attempt=True):
        count = row['attempts'] + int(attempt)
        status = 'attention' if count >= 3 else 'retry'
        with self.connect() as db:
            db.execute("UPDATE sources SET status=?,attempts=?,error=?,due=? WHERE id=? AND digest=?", (status, count, code, time.time() + (min(1800, 60*2**count) if attempt else 5), row['id'], row['digest']))

    def review(self, ident, expected_digest, decision, slug=''):
        with self.connect() as db:
            result = db.execute("UPDATE sources SET review=?,knowledge_slug=? WHERE id=? AND digest=? AND status='analyzed' AND split='examples'", (decision, slug, ident, expected_digest))
            if result.rowcount != 1: raise ValueError('The example changed or is reserved for testing. Refresh first.')

    def stats(self):
        with self.connect() as db:
            rows = db.execute("SELECT origin,status,review,historical,split,count(*) AS count FROM sources GROUP BY origin,status,review,historical,split").fetchall()
            contacts = db.execute('SELECT project_id,count(*) AS count FROM contacts GROUP BY project_id').fetchall()
        return {'groups': [dict(r) for r in rows], 'contacts': [dict(r) for r in contacts]}

    def retry(self, ident):
        with self.connect() as db:
            result = db.execute("UPDATE sources SET status='queued',attempts=0,due=0,error='' WHERE id=? AND status IN ('attention','retry')", (ident,))
            if result.rowcount != 1: raise ValueError('Only retained failed analyses can be retried.')

    def listing(self, limit=50, offset=0):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM sources ORDER BY updated DESC,id LIMIT ? OFFSET ?', (min(100, limit), max(0, offset))).fetchall()
        return [self.decode(r) for r in rows]

    def prune(self, days=30):
        with self.connect() as db:
            db.execute("UPDATE sources SET payload='{}',analysis='{}',status='expired' WHERE updated<? AND status!='expired'", (time.time() - days*86400,))
