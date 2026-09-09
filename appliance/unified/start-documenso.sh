#!/bin/sh
set -eu

. /opt/floodman/aio/common.sh

tries=0
while [ ! -f "$FM_RUN/databases-ready" ]; do
  tries=$((tries + 1))
  [ "$tries" -lt 900 ] || fm_die "Database bootstrap did not finish before Documenso migration recovery."
  sleep 2
done

# The first unified boot could reach this migration before pg_trgm was present.
# Prisma blocks every later migration after that recorded failure. This command
# only changes the exact failed record; it refuses already-applied migrations.
cd /opt/documenso/apps/remix
export PATH="/opt/node22/bin:$PATH"
failed_migration="20260302223702_optimize_recipient_indexes"
recovery_marker="$FM_DATA/documenso/.floodman-recovered-$failed_migration"
if [ ! -f "$recovery_marker" ] && npx --no-install prisma migrate resolve \
    --rolled-back "$failed_migration" \
    --schema ../../packages/prisma/schema.prisma >/dev/null 2>&1; then
  : > "$recovery_marker"
  fm_log "Recovered interrupted Documenso migration $failed_migration for an idempotent retry."
fi

exec /opt/floodman/aio/start-documenso-upstream.sh
