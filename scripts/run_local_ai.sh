#!/usr/bin/env bash
set -euo pipefail
if [[ "${ENABLE_LOCAL_AI_SERVER:-true}" != "true" ]]; then
  exec sleep infinity
fi

AVA_RUNTIME_DIR="${AVA_RUNTIME_DIR:-${DATA_DIR:-/home/container/data}/runtime/ava}"
LOCAL_AI_DIR="${AVA_RUNTIME_DIR}/local_ai_server"
test -f "${LOCAL_AI_DIR}/main.py"
test -f "${LOCAL_AI_DIR}/server.py"

export PYTHONPATH="${LOCAL_AI_DIR}:${AVA_RUNTIME_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${AVA_RUNTIME_DIR}"
exec /opt/venv/bin/python "${LOCAL_AI_DIR}/main.py"
