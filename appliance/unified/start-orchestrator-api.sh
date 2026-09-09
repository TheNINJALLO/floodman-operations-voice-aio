#!/bin/sh
set -eu
. /opt/floodman/aio/common.sh

while [ ! -f "$FM_RUN/databases-ready" ] || [ ! -f "$FM_RUN/gauzy-finalized" ]; do
  sleep 2
done

cd /opt/floodman/orchestrator
export PYTHONPATH="/opt/pydeps/orchestrator:/opt/floodman/orchestrator"
# Port 9004 is the public, path-aware API gateway. Keep the workflow service
# private so /mobile-api can be routed to Office on the same public origin.
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8701 \
  --proxy-headers --forwarded-allow-ips '*' --no-access-log
