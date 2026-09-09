#!/usr/bin/env bash
set -euo pipefail

# Wings can override the image USER and launch the process as namespace root.
# Drop to the image's numeric account before touching persistent data;
# PostgreSQL intentionally refuses to initialize as root.  Use setpriv instead
# of runuser: a failed identity handoff must stop, never recurse into an
# unbounded process tree on a panel-managed container.
if [[ "$(id -u)" == "0" ]]; then
  if [[ "${FLOODMAN_PRIVILEGE_DROP_ATTEMPTED:-}" == "1" ]]; then
    echo "Floodman refused a repeated root privilege handoff." >&2
    exit 1
  fi
  root_data_dir="${DATA_DIR:-/home/container/data}"
  if [[ "${root_data_dir}" != "/home/container/data" ]]; then
    echo "Floodman refused ownership repair outside /home/container/data." >&2
    exit 1
  fi
  mkdir -p "${root_data_dir}"
  # Older panel images ran as root. Repair only mismatched ownership inside the
  # dedicated data mount, without following links or rewriting file contents.
  find -P "${root_data_dir}" -xdev \( ! -uid 988 -o ! -gid 988 \) \
    -exec chown -h 988:988 {} +
  exec setpriv --reuid=988 --regid=988 --clear-groups -- \
    env FLOODMAN_PRIVILEGE_DROP_ATTEMPTED=1 HOME=/home/container \
      USER=container LOGNAME=container "$0" "$@"
fi
unset FLOODMAN_PRIVILEGE_DROP_ATTEMPTED

export DATA_DIR="${DATA_DIR:-/home/container/data}"
# The legacy Pterodactyl egg injects VIRTUAL_ENV=/opt/venv. That path belonged
# to the old voice-only image and must not override the unified image runtime.
export VIRTUAL_ENV=/opt/voice-venv
export PATH="/opt/node24/bin:/opt/python312/bin:/usr/lib/postgresql/14/bin:${VIRTUAL_ENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PYTHONPATH="/opt/voice"
readonly PYTHON_BIN=/opt/voice-venv/bin/python
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
export APP_LOGO="${APP_LOGO:-${FLOODMAN_PUBLIC_URL}/floodman-brand/floodman-wordmark.svg}"

# Pterodactyl mounts its persistent server volume over /home/container, which
# hides links created in the image layer. Recreate only the known Suite
# compatibility links at runtime, and refuse to replace unexpected data.
for business_path in config run logs runtime backups diagnostics tmp; do
  business_target="${DATA_DIR}/business/${business_path}"
  compatibility_path="/home/container/${business_path}"
  mkdir -p "${business_target}"
  if [[ -L "${compatibility_path}" ]]; then
    [[ "$(readlink "${compatibility_path}")" == "data/business/${business_path}" ]] || {
      echo "Refusing to replace unexpected compatibility link: ${compatibility_path}" >&2
      exit 1
    }
  elif [[ -e "${compatibility_path}" ]]; then
    echo "Refusing to replace unexpected persistent path: ${compatibility_path}" >&2
    exit 1
  else
    ln -s "data/business/${business_path}" "${compatibility_path}"
  fi
done

# Initialize the immutable Business Suite browser overlay and RoomFlow payload
# that the standalone Suite egg would otherwise install. Verify the complete
# payload before replacing the prior derived copy; customer and database data
# are never part of this refresh.
business_runtime_zip="/opt/floodman/unified/assets/floodman-operations-runtime-v4.7.2.zip"
business_runtime_dir="${DATA_DIR}/business/runtime/floodman-v4.7.2"
business_overlay="${business_runtime_dir}/app-overlay"
business_overlay_next="${business_runtime_dir}/app-overlay.next"
business_overlay_root="${business_overlay_next}/floodman-operations-v4.7.2"
mkdir -p "${business_runtime_dir}"
rm -rf "${business_overlay_next}"
mkdir -p "${business_overlay_next}"
unzip -q "${business_runtime_zip}" -d "${business_overlay_next}"
[[ "$(cat "${business_overlay_root}/VERSION")" == "4.7.2" ]]
(cd "${business_overlay_root}" && sha256sum -c MANIFEST.sha256 >/dev/null)
rm -rf "${business_overlay}"
mv "${business_overlay_next}" "${business_overlay}"
business_overlay_root="${business_overlay}/floodman-operations-v4.7.2"

# Serve the current Hub source from the pinned Business image so its AI Call
# Center link stays synchronized with this unified release. Pterodactyl's bind
# volume permits content writes but may reject preserving image-layer metadata.
rm -rf "${business_overlay_root}/hub"
mkdir -p "${business_overlay_root}/hub"
cp -R --no-preserve=mode,ownership,timestamps /opt/floodman/hub/. \
  "${business_overlay_root}/hub/"
cp /opt/floodman/unified/assets/floodman-boot-guard.js \
  "${business_runtime_dir}/floodman-boot-guard.js"
cp /opt/floodman/unified/assets/floodman-status.html \
  "${business_runtime_dir}/floodman-status.html"
python3 "${business_overlay_root}/roomflow/prepare-roomflow.py" \
  --target "${DATA_DIR}/roomflow/current" \
  --overlay "${business_overlay_root}/roomflow"
[[ -s "${DATA_DIR}/roomflow/current/index.html" ]]
printf '%s\n' '4.7.2' > "${FM_CONFIG}/floodman-active-runtime.txt"

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
