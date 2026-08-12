#!/usr/bin/env bash
set -euo pipefail
if [[ "${ENABLE_LOCAL_AI_SERVER:-true}" != "true" ]]; then
  exec sleep infinity
fi
AVA_RUNTIME_DIR="${AVA_RUNTIME_DIR:-${DATA_DIR:-/home/container/data}/runtime/ava}"
test -d "${AVA_RUNTIME_DIR}/local_ai_server"
cd "${AVA_RUNTIME_DIR}"
exec /opt/venv/bin/python -m local_ai_server.main
