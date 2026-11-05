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

export DATA_DIR="${DATA_DIR:-/home/container/data}"
RUNTIME_ENV="${RUNTIME_ENV:-${DATA_DIR}/runtime.env}"
mkdir -p "${DATA_DIR}" "${DATA_DIR}/logs" "${DATA_DIR}/runtime" "${DATA_DIR}/models" "${DATA_DIR}/cache"
touch "${RUNTIME_ENV}"
chmod 600 "${RUNTIME_ENV}"

"${PYTHON_BIN}" /opt/floodman/scripts/envfile.py validate "${RUNTIME_ENV}"
"${PYTHON_BIN}" /opt/floodman/scripts/envfile.py normalize "${RUNTIME_ENV}"
eval "$("${PYTHON_BIN}" /opt/floodman/scripts/envfile.py shell-exports "${RUNTIME_ENV}" --missing-only)"

# Wings uses a read-only container filesystem. Model/cache libraries must not
# inherit /root/.cache from the base image or write outside persistent data.
export HOME=/home/container
export XDG_CACHE_HOME="${DATA_DIR}/cache"
export HF_HOME="${DATA_DIR}/models/huggingface"
export HF_HUB_CACHE="${HF_HOME}/hub"
export HF_XET_CACHE="${HF_HOME}/xet"
export HF_ASSETS_CACHE="${HF_HOME}/assets"
export TMPDIR="${DATA_DIR}/cache/tmp"
export TEMP="${TMPDIR}" TMP="${TMPDIR}" TORCH_HOME="${DATA_DIR}/cache/torch"
export LOCAL_DECKARD_BRIEFINGS="${LOCAL_DECKARD_BRIEFINGS:-false}"
if [[ "${LOCAL_VOICE_STACK:-standard}" == "streaming_trial" ]]; then
  export LLAMA_THREADS="${LLAMA_THREADS:-4}"
  export LLAMA_BATCH_THREADS="${LLAMA_BATCH_THREADS:-4}"
fi
mkdir -p "${HF_HUB_CACHE}" "${HF_XET_CACHE}" "${HF_ASSETS_CACHE}" "${TMPDIR}"

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
