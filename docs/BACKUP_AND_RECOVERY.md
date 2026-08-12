# Backup and Recovery

Floodman Operations Voice AIO stores operational data in SQLite and persistent configuration under `DATA_DIR`, normally `/home/container/data`.

## Backup scope

The built-in backup tool includes, when present:

- Floodman operational database
- AVA agent database
- AVA call-history database
- Floodman and AVA configuration
- non-secret Twilio provisioning state
- a manifest with file sizes and SHA-256 digests

By default it excludes:

- `runtime.env`
- `.env`
- `.env.twilio-provisioning`
- recordings
- uploads
- provider credentials stored outside `DATA_DIR/config`

This separation is deliberate. Store runtime and provisioning credentials in encrypted secret storage rather than routinely bundling them with customer data.

## Create a live-safe backup

Docker Compose:

```bash
docker compose exec floodman-voice \
  python /opt/floodman/scripts/backup.py
```

Choose an output directory:

```bash
docker compose exec floodman-voice \
  python /opt/floodman/scripts/backup.py \
  --output-dir /home/container/data/backups
```

The tool uses SQLite’s online backup API and runs `PRAGMA integrity_check` on each copied database. It verifies the staging files, creates a compressed archive, reopens that archive, and verifies every file against the manifest before publishing it.

The final archive is mode `0600`.

## Optional sensitive content

Include uploads and recordings only when required:

```bash
docker compose exec floodman-voice \
  python /opt/floodman/scripts/backup.py \
  --include-media
```

Include generated runtime secrets only for a deliberately encrypted disaster-recovery archive:

```bash
docker compose exec floodman-voice \
  python /opt/floodman/scripts/backup.py \
  --include-runtime-secrets
```

A backup containing media or runtime secrets can contain customer PII and credentials. Encrypt it before it leaves the host, restrict access, and establish a retention policy.

## Off-host copy

List archives:

```bash
docker compose exec floodman-voice \
  sh -lc 'ls -lh /home/container/data/backups'
```

Copy the newest archive to the host:

```bash
CONTAINER_ID=$(docker compose ps -q floodman-voice)
docker cp \
  "${CONTAINER_ID}:/home/container/data/backups/<archive>.tar.gz" \
  ./backups/
```

Then copy it to independent encrypted storage. A backup that exists only on the voice server does not protect against host loss.

## Suggested schedule

Example host cron entry for a daily backup:

```cron
17 2 * * * cd /opt/floodman-operations-voice-aio && docker compose exec -T floodman-voice python /opt/floodman/scripts/backup.py --retention-days 30 >> /var/log/floodman-backup.log 2>&1
```

The built-in retention option deletes matching local backup archives older than the configured number of days. Off-host retention must be managed separately.

## Pterodactyl backup

Run from the server console:

```bash
python /opt/floodman/scripts/backup.py \
  --data-dir /home/container/data \
  --output-dir /home/container/data/backups
```

Download the resulting archive through SFTP or another secured file-transfer route. Pterodactyl’s own backup feature may also be used, but the application backup provides SQLite-consistent copies and a verifiable manifest.

## Restore preparation

A restore should be tested into a separate instance before an emergency.

Keep available:

- application release matching the backup manifest version
- database/configuration archive
- encrypted runtime `.env`
- encrypted Twilio provisioning environment when resource changes are needed
- SIP TLS certificates and private key, if used
- Roomflow and AVA provider credentials
- public DNS and firewall documentation

## Restore procedure

1. Stop the instance so no process writes to the target databases.

```bash
docker compose down
```

2. Copy the archive to a restricted staging directory and inspect it without writing into the live volume.

```bash
mkdir -m 700 restore-staging
tar -xzf <archive>.tar.gz -C restore-staging
```

3. Read `manifest.json` and verify the archive SHA-256 from the backup job or trusted inventory.

4. Preserve the damaged data directory before replacing anything.

5. Restore the database files to their expected locations:

```text
/home/container/data/floodman-voice.sqlite3
/home/container/data/ava/operator/agents.db
/home/container/data/ava/call_history.db
```

6. Restore the configuration directory to:

```text
/home/container/data/config
```

7. Restore non-secret Twilio state to:

```text
/home/container/data/twilio/provisioning.json
```

8. Restore `runtime.env`, `.env`, provider credentials, and TLS keys from the separate encrypted secret backup when needed.

9. Set ownership and permissions. The container image uses UID and GID 988:

```bash
sudo chown -R 988:988 /path/to/restored/data
sudo find /path/to/restored/data -type d -exec chmod 700 {} +
sudo find /path/to/restored/data -type f -exec chmod 600 {} +
```

10. Start the instance with SIP and outbound campaigns disabled for inspection:

```dotenv
SIP_TRUNK_MODE=disabled
OUTBOUND_ENABLED=false
ROOMFLOW_ENABLED=false
```

11. Start the application:

```bash
docker compose up -d
```

12. Verify:

```bash
curl -fsS http://127.0.0.1:9000/livez

docker compose exec floodman-voice \
  python /opt/floodman/scripts/preflight.py
```

13. Inspect authenticated diagnostics, customer counts, agents, appointments, outbox entries, and recent calls.

14. Enable the SIP trunk and complete an operator echo call and a direct inbound test.

15. Re-enable Roomflow only after record reconciliation is reviewed.

16. Re-enable outbound campaigns last.

## Database integrity checks after restore

Inside the container:

```bash
python - <<'PY'
import sqlite3
from pathlib import Path

paths = [
    Path('/home/container/data/floodman-voice.sqlite3'),
    Path('/home/container/data/ava/operator/agents.db'),
    Path('/home/container/data/ava/call_history.db'),
]
for path in paths:
    if not path.exists():
        continue
    with sqlite3.connect(path) as db:
        print(path, db.execute('PRAGMA integrity_check').fetchone()[0])
PY
```

Every present database should report `ok`.

## Recovery objectives

Define Floodman’s policy explicitly. A reasonable starting target for a single-node deployment is:

- Recovery point objective: no more than one business day of database changes
- Recovery time objective: restore a known-good temporary DID and human fallback before restoring AI and outbound workflows

The telephone fallback route matters more than restoring every analytical feature at once. In an outage, restore the ability to answer and transfer calls first, then Roomflow synchronization, AI automation, recordings, and outbound campaigns.
