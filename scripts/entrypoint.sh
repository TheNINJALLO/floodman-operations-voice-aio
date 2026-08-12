#!/usr/bin/env bash
set -euo pipefail

export DATA_DIR="${DATA_DIR:-/home/container/data}"
export CONFIG_DIR="${CONFIG_DIR:-${DATA_DIR}/config}"
export ASTERISK_CONFIG_DIR="${ASTERISK_CONFIG_DIR:-${DATA_DIR}/asterisk/etc}"
export AGENTS_DB_PATH="${AGENTS_DB_PATH:-${DATA_DIR}/ava/operator/agents.db}"
export CALL_HISTORY_DB_PATH="${CALL_HISTORY_DB_PATH:-${DATA_DIR}/ava/call_history.db}"
export DATABASE_PATH="${DATABASE_PATH:-${DATA_DIR}/floodman-voice.sqlite3}"
export HF_HOME="${HF_HOME:-${DATA_DIR}/model-cache/huggingface}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${DATA_DIR}/cache}"
SUPERVISOR_STATE_DIR="${DATA_DIR}/runtime/supervisor"
export SUPERVISOR_LOGFILE="${SUPERVISOR_LOGFILE:-${SUPERVISOR_STATE_DIR}/supervisord.log}"
export SUPERVISOR_PIDFILE="${SUPERVISOR_PIDFILE:-${SUPERVISOR_STATE_DIR}/supervisord.pid}"
export SUPERVISOR_CHILDLOGDIR="${SUPERVISOR_CHILDLOGDIR:-${SUPERVISOR_STATE_DIR}}"

mkdir -p \
  "${DATA_DIR}/logs" \
  "${DATA_DIR}/ava/operator" \
  "${DATA_DIR}/models" \
  "${DATA_DIR}/model-cache" \
  "${DATA_DIR}/cache" \
  "${DATA_DIR}/uploads" \
  "${DATA_DIR}/recordings" \
  "${CONFIG_DIR}/ava" \
  "${ASTERISK_CONFIG_DIR}" \
  "${SUPERVISOR_CHILDLOGDIR}"

RUNTIME_ENV="${DATA_DIR}/runtime.env"
if [[ -f "${RUNTIME_ENV}" ]]; then
  # shellcheck disable=SC1090
  source "${RUNTIME_ENV}"
fi

touch "${RUNTIME_ENV}"
chmod 600 "${RUNTIME_ENV}"

make_secret() {
  local name="$1"
  local current="${!name:-}"
  if [[ -z "${current}" ]]; then
    current="$(/opt/venv/bin/python - <<'PY'
import secrets
print(secrets.token_urlsafe(42))
PY
)"
    export "${name}=${current}"
    printf 'export %s=%q\n' "${name}" "${current}" >> "${RUNTIME_ENV}"
  fi
}

make_secret ADMIN_TOKEN
make_secret INTERNAL_TOKEN
make_secret UPLOAD_TOKEN_SECRET
make_secret ARI_SECRET
make_secret AMI_SECRET

export WEB_PORT="${SERVER_PORT:-${WEB_PORT:-9000}}"
export WEB_HOST="${WEB_HOST:-0.0.0.0}"
export SIP_PORT="${SIP_PORT:-5060}"
export RTP_START="${RTP_START:-10000}"
export RTP_END="${RTP_END:-10040}"
export ARI_PORT="${ARI_PORT:-8088}"
export AMI_PORT="${AMI_PORT:-5038}"
export GATE_PORT="${GATE_PORT:-9019}"
export ASTERISK_HOST="${ASTERISK_HOST:-127.0.0.1}"
export ASTERISK_ARI_PORT="${ASTERISK_ARI_PORT:-${ARI_PORT}}"
export ASTERISK_ARI_SCHEME="${ASTERISK_ARI_SCHEME:-http}"
export ASTERISK_ARI_USERNAME="${ASTERISK_ARI_USERNAME:-${ARI_USERNAME:-floodman-ava}}"
export ASTERISK_ARI_PASSWORD="${ASTERISK_ARI_PASSWORD:-${ARI_SECRET}}"
export ARI_USERNAME="${ARI_USERNAME:-${ASTERISK_ARI_USERNAME}}"
export ARI_PASSWORD="${ARI_PASSWORD:-${ARI_SECRET}}"
export AMI_USERNAME="${AMI_USERNAME:-floodman}"
export FLOODMAN_INTERNAL_URL="${FLOODMAN_INTERNAL_URL:-http://127.0.0.1:${WEB_PORT}}"
export LOCAL_WS_URL="${LOCAL_WS_URL:-ws://127.0.0.1:8765}"
export LOCAL_WS_HOST="${LOCAL_WS_HOST:-127.0.0.1}"
export LOCAL_WS_PORT="${LOCAL_WS_PORT:-8765}"
export DEFAULT_TIMEZONE="${DEFAULT_TIMEZONE:-America/Detroit}"
export TZ="${TZ:-${DEFAULT_TIMEZONE}}"
export PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-http://127.0.0.1:${WEB_PORT}}"
export TRUSTED_PROXY_IPS="${TRUSTED_PROXY_IPS:-127.0.0.1}"
export ENABLE_API_DOCS="${ENABLE_API_DOCS:-false}"
export PRINT_BOOTSTRAP_SECRETS="${PRINT_BOOTSTRAP_SECRETS:-false}"
export STARTUP_PREFLIGHT="${STARTUP_PREFLIGHT:-warn}"
if [[ -z "${TRUSTED_HOSTS:-}" ]]; then
  PUBLIC_HOST="$(/opt/venv/bin/python - <<'PYHOST'
import os
from urllib.parse import urlparse
print(urlparse(os.environ.get("PUBLIC_BASE_URL", "")).hostname or "")
PYHOST
)"
  export TRUSTED_HOSTS="localhost,127.0.0.1${PUBLIC_HOST:+,${PUBLIC_HOST}}"
fi

if [[ ! -f "${CONFIG_DIR}/floodman.yaml" ]]; then
  cp /opt/floodman/config/floodman.yaml "${CONFIG_DIR}/floodman.yaml"
fi
if [[ ! -f "${CONFIG_DIR}/ava/ai-agent.local.yaml" ]]; then
  cp /opt/floodman/config/ava/ai-agent.local.yaml "${CONFIG_DIR}/ava/ai-agent.local.yaml"
fi
cp "${CONFIG_DIR}/ava/ai-agent.local.yaml" /opt/ava/config/ai-agent.local.yaml

# Keep model downloads and AVA state on persistent storage.
if [[ ! -f "${DATA_DIR}/models/registry.json" && -f /opt/ava/models/registry.json ]]; then
  cp /opt/ava/models/registry.json "${DATA_DIR}/models/registry.json"
fi
if [[ ! -L /opt/ava/models ]]; then
  rm -rf /opt/ava/models
  ln -s "${DATA_DIR}/models" /opt/ava/models
fi
if [[ -d /opt/model-cache && ! -f "${DATA_DIR}/model-cache/.seeded" ]]; then
  cp -a /opt/model-cache/. "${DATA_DIR}/model-cache/" 2>/dev/null || true
  touch "${DATA_DIR}/model-cache/.seeded"
fi

if [[ "${ASTERISK_MODE:-embedded}" == "embedded" ]]; then
  /opt/venv/bin/python /opt/floodman/scripts/render_asterisk.py
  AGI_DIR="${DATA_DIR}/asterisk/agi-bin"
  mkdir -p "${AGI_DIR}"
  cp /opt/floodman/scripts/agi_common.py "${AGI_DIR}/"
  cp /opt/floodman/scripts/agi_gate_start.py "${AGI_DIR}/"
  cp /opt/floodman/scripts/agi_gate_finish.py "${AGI_DIR}/"
  chmod 755 "${AGI_DIR}"/agi_*.py
  mkdir -p "${DATA_DIR}/asterisk/varlib"
  SOUND_SOURCE=""
  for candidate in /usr/share/asterisk/sounds /var/lib/asterisk/sounds; do
    if [[ -d "${candidate}" ]]; then SOUND_SOURCE="${candidate}"; break; fi
  done
  if [[ -n "${SOUND_SOURCE}" && ! -e "${DATA_DIR}/asterisk/varlib/sounds" ]]; then
    ln -s "${SOUND_SOURCE}" "${DATA_DIR}/asterisk/varlib/sounds"
  fi
fi

if [[ "${AUTO_INSTALL_LOCAL_MODELS:-false}" == "true" && ! -f "${DATA_DIR}/models/.floodman-models-ready" ]]; then
  /opt/ava/scripts/model_setup.sh \
    --tier "${LOCAL_MODEL_TIER:-LIGHT}" \
    --assume-yes \
    --language "${LOCAL_MODEL_LANGUAGE:-en-US}"
  touch "${DATA_DIR}/models/.floodman-models-ready"
fi

case "${STARTUP_PREFLIGHT,,}" in
  off|false|0) ;;
  strict) /opt/venv/bin/python /opt/floodman/scripts/preflight.py --no-network --strict ;;
  warn|true|1) /opt/venv/bin/python /opt/floodman/scripts/preflight.py --no-network || true ;;
  *) echo "Invalid STARTUP_PREFLIGHT=${STARTUP_PREFLIGHT}; use off, warn, or strict" >&2; exit 2 ;;
esac

ADMIN_TOKEN_LINE="Admin token: stored in ${RUNTIME_ENV} (not printed)"
if [[ "${PRINT_BOOTSTRAP_SECRETS,,}" == "true" ]]; then
  ADMIN_TOKEN_LINE="Admin token: ${ADMIN_TOKEN}"
fi

cat <<BANNER
============================================================
 Floodman Operations Voice AIO ${FLOODMAN_VERSION:-1.1.1}
 Never Miss A Call Again With Floodman's 24/7 AI Receptionist
 Web panel: ${PUBLIC_BASE_URL}
 ${ADMIN_TOKEN_LINE}
 Gate AudioSocket: 127.0.0.1:${GATE_PORT}
 SIP mode: ${SIP_TRUNK_MODE:-disabled}
 Asterisk mode: ${ASTERISK_MODE:-embedded}
 AVA provider: ${AVA_PROVIDER:-local_hybrid}
 Persistent data: ${DATA_DIR}
============================================================
BANNER

exec /usr/bin/supervisord -c /opt/floodman/supervisord.conf
