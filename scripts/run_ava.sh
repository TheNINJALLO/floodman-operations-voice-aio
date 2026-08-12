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
AVA_RUNTIME_DIR="${AVA_RUNTIME_DIR:-${DATA_DIR:-/home/container/data}/runtime/ava}"
test -f "${AVA_RUNTIME_DIR}/main.py"
cd "${AVA_RUNTIME_DIR}"
exec /opt/venv/bin/python "${AVA_RUNTIME_DIR}/main.py"
