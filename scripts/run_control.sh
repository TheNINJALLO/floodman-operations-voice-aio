#!/usr/bin/env bash
set -euo pipefail
cd /opt/floodman
exec /opt/venv/bin/uvicorn app.main:app \
  --app-dir /opt/floodman \
  --host "${WEB_HOST:-0.0.0.0}" \
  --port "${WEB_PORT:-9000}" \
  --proxy-headers \
  --forwarded-allow-ips="${TRUSTED_PROXY_IPS:-127.0.0.1}"
