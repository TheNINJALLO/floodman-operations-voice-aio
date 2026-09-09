#!/bin/sh
set -eu
. /opt/floodman/aio/common.sh

fm_log "Waiting for embedded PostgreSQL..."
tries=0
until pg_isready -h "$FM_RUN/postgres" -p 5432 -U postgres >/dev/null 2>&1; do
  tries=$((tries + 1))
  [ "$tries" -lt 180 ] || fm_die "PostgreSQL did not become ready."
  sleep 2
done

export PGPASSWORD="$POSTGRES_ADMIN_PASSWORD"
PSQL="psql -h $FM_RUN/postgres -p 5432 -U postgres -d postgres -v ON_ERROR_STOP=1"

$PSQL \
  --set=floodman_pass="$FLOODMAN_DB_PASSWORD" \
  --set=gauzy_pass="$GAUZY_DB_PASSWORD" \
  --set=documenso_pass="$DOCUMENSO_DB_PASSWORD" \
  --set=ai_pass="$AI_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE floodman LOGIN PASSWORD %L', :'floodman_pass')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='floodman') \gexec
SELECT format('ALTER ROLE floodman LOGIN PASSWORD %L', :'floodman_pass') \gexec

SELECT format('CREATE ROLE gauzy LOGIN PASSWORD %L', :'gauzy_pass')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='gauzy') \gexec
SELECT format('ALTER ROLE gauzy LOGIN PASSWORD %L', :'gauzy_pass') \gexec

SELECT format('CREATE ROLE documenso LOGIN PASSWORD %L', :'documenso_pass')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='documenso') \gexec
SELECT format('ALTER ROLE documenso LOGIN PASSWORD %L', :'documenso_pass') \gexec

SELECT format('CREATE ROLE floodman_intel LOGIN PASSWORD %L', :'ai_pass')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='floodman_intel') \gexec
SELECT format('ALTER ROLE floodman_intel LOGIN PASSWORD %L', :'ai_pass') \gexec

SELECT 'CREATE DATABASE floodman OWNER floodman'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='floodman') \gexec
SELECT 'CREATE DATABASE gauzy OWNER gauzy'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='gauzy') \gexec
SELECT 'CREATE DATABASE documenso OWNER documenso'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='documenso') \gexec
SQL

# PostgreSQL 14 creates public under the administrator and grants CREATE through
# PUBLIC. The restricted AI grants below revoke that shared privilege, so retain
# an explicit application-owner grant before every idempotent migration pass.
psql -h "$FM_RUN/postgres" -p 5432 -U postgres -d floodman \
  -v ON_ERROR_STOP=1 -c 'GRANT USAGE, CREATE ON SCHEMA public TO floodman'

# Apply the application baseline as the floodman database owner so every table,
# sequence, function, and extension is writable by the runtime application.
export PGPASSWORD="$FLOODMAN_DB_PASSWORD"
baseline="$(psql -h 127.0.0.1 -p 5432 -U floodman -d floodman -Atc "SELECT baseline_version FROM floodman_schema_info WHERE singleton=true" 2>/dev/null || true)"
if [ "$baseline" != "3.0.0" ]; then
  fm_log "Applying the fixed Floodman v3 database baseline as the application owner..."
  psql -h 127.0.0.1 -p 5432 -U floodman -d floodman -v ON_ERROR_STOP=1 -f /opt/floodman/database/init/00-floodman-baseline.sql
fi

# Additive migrations are versioned separately from the immutable fresh-install
# baseline. Rollback files are documentation/operator tools and are never run.
for migration in /opt/floodman/database/migrations/[0-9][0-9][0-9]_*.sql; do
  [ -f "$migration" ] || continue
  case "$migration" in *.rollback.sql) continue ;; esac
  fm_log "Applying additive database migration $(basename "$migration")..."
  psql -h 127.0.0.1 -p 5432 -U floodman -d floodman -v ON_ERROR_STOP=1 -f "$migration"
done

# The restricted intelligence grants require the PostgreSQL administrator.
export PGPASSWORD="$POSTGRES_ADMIN_PASSWORD"
psql -h "$FM_RUN/postgres" -p 5432 -U postgres -d floodman \
  --set=ON_ERROR_STOP=1 --set=ai_pass="$AI_DB_PASSWORD" <<'SQL'
SELECT format('ALTER ROLE floodman_intel LOGIN PASSWORD %L', :'ai_pass') \gexec
GRANT CONNECT ON DATABASE floodman TO floodman_intel;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO floodman_intel;
GRANT SELECT,INSERT,UPDATE ON TABLE competitor_targets,competitor_snapshots,competitor_reports TO floodman_intel;
REVOKE ALL ON TABLE workflow_jobs,workflow_documents,workflow_payments,external_mappings,webhook_events,idempotency_keys,audit_events,outbox_events,portal_access_log,ar_cases,message_threads,message_events,payment_promises,collection_holds,ai_message_decisions,staff_alerts,ar_digest_runs,communication_consents,call_intakes,call_intake_events FROM floodman_intel;
SQL

touch "$FM_RUN/databases-ready"
fm_log "Embedded databases are ready."
