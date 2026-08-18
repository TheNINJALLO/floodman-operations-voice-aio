#!/usr/bin/env bash
set -euo pipefail

truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

# Floodman production profile local-model guard.
case "${FLOODMAN_AI_PROFILE:-auto}" in
  production|production_hybrid|floodman_production)
    echo "Floodman local AI disabled because production_hybrid is selected"
    exec sleep infinity
    ;;
esac

if ! truthy "${ENABLE_LOCAL_AI_SERVER:-true}"; then
  echo "Floodman local AI disabled by ENABLE_LOCAL_AI_SERVER=${ENABLE_LOCAL_AI_SERVER:-false}"
  exec sleep infinity
fi

AVA_RUNTIME_DIR="${AVA_RUNTIME_DIR:-${DATA_DIR:-/home/container/data}/runtime/ava}"
LOCAL_AI_DIR="${AVA_RUNTIME_DIR}/local_ai_server"
test -f "${LOCAL_AI_DIR}/main.py"
test -f "${LOCAL_AI_DIR}/server.py"

export PYTHONPATH="${LOCAL_AI_DIR}:${AVA_RUNTIME_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${AVA_RUNTIME_DIR}"

echo "Starting Floodman local AI in ${LOCAL_AI_MODE:-auto} mode on ${LOCAL_WS_HOST:-127.0.0.1}:${LOCAL_WS_PORT:-8765}"
exec /opt/venv/bin/python "${LOCAL_AI_DIR}/main.py"
