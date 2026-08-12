#!/usr/bin/env bash
set -euo pipefail
if [[ "${AVA_ENABLED:-true}" != "true" ]]; then
  exec sleep infinity
fi
if [[ "${ASTERISK_MODE:-embedded}" == "embedded" ]]; then
  for _ in $(seq 1 90); do
    if curl -fsS -u "${ARI_USERNAME}:${ARI_SECRET}" \
      "http://127.0.0.1:${ARI_PORT:-8088}/ari/asterisk/info" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi
cd /opt/ava
exec /opt/venv/bin/python /opt/ava/main.py
