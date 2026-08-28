#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="${PYTHONPATH:-/opt/floodman}"
export VIRTUAL_ENV="${VIRTUAL_ENV:-/opt/venv}"
export PATH="${VIRTUAL_ENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
PYTHON_BIN="${PYTHON_BIN:-${VIRTUAL_ENV}/bin/python}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
  echo "Floodman startup error: expected Python at ${PYTHON_BIN}, but it is unavailable." >&2
  echo "This container image is incomplete and must be rebuilt." >&2
  exit 127
fi

echo "Floodman Python runtime: $("${PYTHON_BIN}" --version 2>&1) at ${PYTHON_BIN}"

DATA_DIR="${DATA_DIR:-/home/container/data}"
RUNTIME_ENV="${RUNTIME_ENV:-${DATA_DIR}/runtime.env}"
mkdir -p "${DATA_DIR}" "${DATA_DIR}/logs" "${DATA_DIR}/runtime" "${DATA_DIR}/models" "${DATA_DIR}/cache"
touch "${RUNTIME_ENV}"
chmod 600 "${RUNTIME_ENV}"

"${PYTHON_BIN}" /opt/floodman/scripts/envfile.py validate "${RUNTIME_ENV}"
"${PYTHON_BIN}" /opt/floodman/scripts/envfile.py normalize "${RUNTIME_ENV}"
eval "$("${PYTHON_BIN}" /opt/floodman/scripts/envfile.py shell-exports "${RUNTIME_ENV}" --missing-only)"

if [[ ! -d "${DATA_DIR}/knowledge" ]]; then
  cp -a /opt/floodman/knowledge "${DATA_DIR}/knowledge"
fi
if [[ ! -f "${DATA_DIR}/service_area.yaml" ]]; then
  cp /opt/floodman/config/service_area.yaml "${DATA_DIR}/service_area.yaml"
fi

/opt/floodman/scripts/gpu-preflight.sh
"${PYTHON_BIN}" /opt/floodman/scripts/download_models.py
"${PYTHON_BIN}" /opt/floodman/scripts/preflight.py
"${PYTHON_BIN}" /opt/floodman/scripts/render_asterisk.py

exec /usr/bin/supervisord -c /opt/floodman/supervisor/supervisord.conf
