#!/usr/bin/env bash
set -euo pipefail

export DATA_DIR="${DATA_DIR:-/home/container/data}"
export VIRTUAL_ENV="${VIRTUAL_ENV:-/opt/voice-venv}"
export PATH="/opt/node24/bin:/usr/lib/postgresql/14/bin:${VIRTUAL_ENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONPATH="/opt/voice"
PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
RUNTIME_ENV="${RUNTIME_ENV:-${DATA_DIR}/runtime.env}"

mkdir -p "${DATA_DIR}" "${DATA_DIR}/logs" "${DATA_DIR}/runtime" "${DATA_DIR}/models" \
  "${DATA_DIR}/cache" "${DATA_DIR}/business"
touch "${RUNTIME_ENV}"
chmod 0600 "${RUNTIME_ENV}"

"${PYTHON_BIN}" /opt/floodman/scripts/envfile.py validate "${RUNTIME_ENV}"
"${PYTHON_BIN}" /opt/floodman/scripts/envfile.py normalize "${RUNTIME_ENV}"
eval "$("${PYTHON_BIN}" /opt/floodman/scripts/envfile.py shell-exports "${RUNTIME_ENV}" --missing-only)"

export CONFIG_DIR="/opt/voice/config"
export KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-${DATA_DIR}/knowledge}"
export SERVICE_AREA_PATH="${SERVICE_AREA_PATH:-${DATA_DIR}/service_area.yaml}"
export AUDIOSOCKET_PORT=8091
export WEB_PORT=8802
export VOICE_PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-https://aicall.oninetwork.com}"
export BUSINESS_SUITE_EVENTS_ENABLED=true
export BUSINESS_SUITE_EVENTS_URL="http://127.0.0.1:9004/webhooks/ai-calling/deterministic"
export BUSINESS_SUITE_PUBLIC_URL="${BUSINESS_SUITE_PUBLIC_URL:-https://floodman.oninetwork.com}"
export BUSINESS_SUITE_ORGANIZATION_ID="${BUSINESS_SUITE_ORGANIZATION_ID:-floodman}"
export BUSINESS_SUITE_WORKSPACE_ID="${BUSINESS_SUITE_WORKSPACE_ID:-${P_SERVER_UUID:-floodman-operations}}"

if [[ ! -d "${KNOWLEDGE_DIR}" ]]; then
  cp -a /opt/voice/knowledge "${KNOWLEDGE_DIR}"
fi
if [[ ! -f "${SERVICE_AREA_PATH}" ]]; then
  cp /opt/voice/config/service_area.yaml "${SERVICE_AREA_PATH}"
fi

/opt/floodman/scripts/gpu-preflight.sh
"${PYTHON_BIN}" /opt/floodman/scripts/download_models.py
"${PYTHON_BIN}" /opt/floodman/scripts/preflight.py
"${PYTHON_BIN}" /opt/floodman/scripts/render_asterisk.py

# Preserve the exact Voice/Pterodactyl environment before the Business Suite
# assigns its own SMTP, Twilio sandbox, Node, public URL, and WEB_PORT values.
export -p > "${DATA_DIR}/runtime/voice-process.env"
chmod 0600 "${DATA_DIR}/runtime/voice-process.env"

export FM_HOME=/home/container
export FM_CONFIG="${DATA_DIR}/business/config"
export FM_DATA="${DATA_DIR}/business"
export FM_RUN="${DATA_DIR}/business/run"
export FM_LOGS="${DATA_DIR}/business/logs"
export SERVER_PORT=9000
export DOCUMENSO_PORT=9001
export MAILPIT_PORT=9002
export ENGINEERING_PORT=9003
export FLOODMAN_API_PORT=9004
export FLOODMAN_PUBLIC_SCHEME=https
export FLOODMAN_PUBLIC_HOST=floodman.oninetwork.com
export FLOODMAN_PUBLIC_URL="${FLOODMAN_PUBLIC_URL:-https://floodman.oninetwork.com}"
export FLOODMAN_DOCUMENSO_URL="${FLOODMAN_DOCUMENSO_URL:-https://sign.oninetwork.com}"
export FLOODMAN_MAILPIT_URL="${FLOODMAN_MAILPIT_URL:-http://127.0.0.1:9002}"
export FLOODMAN_ENGINEERING_URL="${FLOODMAN_ENGINEERING_URL:-https://lab.oninetwork.com}"
export FLOODMAN_API_PUBLIC_URL="${FLOODMAN_API_PUBLIC_URL:-https://api.oninetwork.com}"
export FLOODMAN_VOICE_URL="${FLOODMAN_VOICE_URL:-${VOICE_PUBLIC_BASE_URL}}"

mkdir -p "${FM_RUN}" "${FM_LOGS}" "${DATA_DIR}/business/tmp/gateway-client" \
  "${DATA_DIR}/business/tmp/gateway-proxy" "${DATA_DIR}/business/tmp/gateway-fastcgi" \
  "${DATA_DIR}/business/tmp/gateway-uwsgi" "${DATA_DIR}/business/tmp/gateway-scgi"

owner_file="${FM_CONFIG}/unified-owner.env"
mkdir -p "${FM_CONFIG}"
owner_first_override="${FLOODMAN_OWNER_FIRST_NAME:-}"
owner_last_override="${FLOODMAN_OWNER_LAST_NAME:-}"
owner_email_override="${FLOODMAN_OWNER_EMAIL:-}"
owner_password_override="${FLOODMAN_OWNER_PASSWORD:-}"
if [[ -s "${owner_file}" ]]; then
  # shellcheck disable=SC1090
  source "${owner_file}"
fi
[[ -n "${owner_first_override}" ]] && FLOODMAN_OWNER_FIRST_NAME="${owner_first_override}"
[[ -n "${owner_last_override}" ]] && FLOODMAN_OWNER_LAST_NAME="${owner_last_override}"
[[ -n "${owner_email_override}" ]] && FLOODMAN_OWNER_EMAIL="${owner_email_override}"
[[ -n "${owner_password_override}" ]] && FLOODMAN_OWNER_PASSWORD="${owner_password_override}"
export FLOODMAN_OWNER_FIRST_NAME="${FLOODMAN_OWNER_FIRST_NAME:-Floodman}"
export FLOODMAN_OWNER_LAST_NAME="${FLOODMAN_OWNER_LAST_NAME:-Owner}"
export FLOODMAN_OWNER_EMAIL="${FLOODMAN_OWNER_EMAIL:-owner@floodman.local}"
export FLOODMAN_OWNER_PASSWORD="${FLOODMAN_OWNER_PASSWORD:-$(openssl rand -hex 18)}"
{
  printf 'export FLOODMAN_OWNER_FIRST_NAME=%q\n' "${FLOODMAN_OWNER_FIRST_NAME}"
  printf 'export FLOODMAN_OWNER_LAST_NAME=%q\n' "${FLOODMAN_OWNER_LAST_NAME}"
  printf 'export FLOODMAN_OWNER_EMAIL=%q\n' "${FLOODMAN_OWNER_EMAIL}"
  printf 'export FLOODMAN_OWNER_PASSWORD=%q\n' "${FLOODMAN_OWNER_PASSWORD}"
} > "${owner_file}"
chmod 0600 "${owner_file}"

exec /opt/floodman/aio/start-suite.sh
