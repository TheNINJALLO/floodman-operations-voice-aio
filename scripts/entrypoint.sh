#!/usr/bin/env bash
set -euo pipefail

export DATA_DIR="${DATA_DIR:-/home/container/data}"
export CONFIG_DIR="${CONFIG_DIR:-${DATA_DIR}/config}"
export KNOWLEDGE_DIR="${KNOWLEDGE_DIR:-${DATA_DIR}/knowledge}"
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
  "${KNOWLEDGE_DIR}" \
  "${CONFIG_DIR}/ava" \
  "${ASTERISK_CONFIG_DIR}" \
  "${SUPERVISOR_CHILDLOGDIR}"

RUNTIME_ENV="${DATA_DIR}/runtime.env"
if [[ -f "${RUNTIME_ENV}" ]]; then
  while IFS= read -r line || [[ -n "${line}" ]]; do
    export "${line}"
  done < <(
    PYTHONPATH=/opt/floodman/scripts /opt/venv/bin/python - "${RUNTIME_ENV}" <<'PY'
import os
import sys

from envfile import load_env_file

before = dict(os.environ)
load_env_file(sys.argv[1], override=True, required=True)
for key, value in sorted(os.environ.items()):
    if before.get(key) != value:
        print(f"{key}={value}")
PY
  )
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
export AVA_IMAGE_DIR="${AVA_IMAGE_DIR:-/opt/ava}"
export AVA_RUNTIME_DIR="${AVA_RUNTIME_DIR:-${DATA_DIR}/runtime/ava}"

# Floodman writable runtime cache normalization. The image seeds model caches
# under /opt/model-cache, but Pterodactyl may mount /opt read-only.
normalize_writable_cache_path() {
  local current="$1"
  local fallback="$2"
  case "${current}" in
    ""|/opt/model-cache|/opt/model-cache/*) printf '%s\n' "${fallback}" ;;
    *) printf '%s\n' "${current}" ;;
  esac
}

export HF_HOME="$(normalize_writable_cache_path "${HF_HOME:-}" "${DATA_DIR}/model-cache/huggingface")"
export XDG_CACHE_HOME="$(normalize_writable_cache_path "${XDG_CACHE_HOME:-}" "${DATA_DIR}/cache")"
export HF_HUB_CACHE="$(normalize_writable_cache_path "${HF_HUB_CACHE:-}" "${HF_HOME}/hub")"
export HUGGINGFACE_HUB_CACHE="$(normalize_writable_cache_path "${HUGGINGFACE_HUB_CACHE:-}" "${HF_HUB_CACHE}")"
mkdir -p "${HF_HOME}" "${XDG_CACHE_HOME}" "${HF_HUB_CACHE}"
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

# Install the reviewed website knowledge pack once per version. Existing operational
# settings and custom knowledge are preserved, and managed files are backed up first.
/opt/venv/bin/python /opt/floodman/scripts/install_knowledge_pack.py \
  --pack-version "${KNOWLEDGE_PACK_VERSION:-2026-08-13.2}"

# Never mutate /opt/ava at runtime. Pterodactyl may mount the image root read-only.
/opt/floodman/scripts/prepare_ava_runtime.sh

# AVA config and model persistence are prepared in the writable runtime worktree.
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
  cp /opt/floodman/scripts/agi_record_finalize.py "${AGI_DIR}/"
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

truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

# Floodman telephone responsiveness profile.
#
# Direct calls fail open to the greeting quickly, while the call-gate
# server keeps Google-hinted calls on the longer inspection window.
# Vosk's old 5000 ms idle finalizer made a completed caller turn feel
# unanswered; 700 ms is long enough for natural pauses but fast enough
# for telephone turn-taking.
if truthy "${FLOODMAN_LOW_LATENCY_MODE:-true}"; then
  export GATE_MIN_SECONDS="${FLOODMAN_GATE_MIN_SECONDS:-0.55}"
  export GATE_TRANSCRIBE_INTERVAL_SECONDS="${FLOODMAN_GATE_TRANSCRIBE_INTERVAL_SECONDS:-0.55}"
  export GATE_NO_SPEECH_TIMEOUT_SECONDS="${FLOODMAN_DIRECT_GREETING_DELAY_SECONDS:-1.4}"

  export LOCAL_STT_BACKEND="${LOCAL_STT_BACKEND:-vosk}"
  export LOCAL_STT_IDLE_MS="${LOCAL_STT_IDLE_MS:-700}"

  export LOCAL_LLM_CHAT_FORMAT="${LOCAL_LLM_CHAT_FORMAT:-auto}"
  export LOCAL_LLM_CONTEXT="${LOCAL_LLM_CONTEXT:-2048}"
  export LOCAL_LLM_MAX_TOKENS="${LOCAL_LLM_MAX_TOKENS:-48}"
  export LOCAL_LLM_INFER_TIMEOUT_SEC="${LOCAL_LLM_INFER_TIMEOUT_SEC:-30}"
  export LOCAL_LLM_STREAMING_TTS_OVERLAP="${LOCAL_LLM_STREAMING_TTS_OVERLAP:-1}"

  export LOCAL_ENABLE_FILLER_AUDIO="${LOCAL_ENABLE_FILLER_AUDIO:-1}"
  export LOCAL_FILLER_PHRASES="${LOCAL_FILLER_PHRASES:-Absolutely. Give me just a moment.,I can help with that.,Let me check that for you.}"
  export LOCAL_TTS_PHRASE_CACHE_ENABLED="${LOCAL_TTS_PHRASE_CACHE_ENABLED:-1}"
  export LOCAL_TTS_PHRASE_CACHE_MAX_LEN="${LOCAL_TTS_PHRASE_CACHE_MAX_LEN:-240}"
fi

normalize_local_model_tier() {
  case "${1^^}" in
    LIGHT|LIGHT_CPU) printf '%s\n' "LIGHT_CPU" ;;
    BALANCED|MEDIUM|MEDIUM_CPU) printf '%s\n' "MEDIUM_CPU" ;;
    QUALITY|HEAVY|HEAVY_CPU) printf '%s\n' "HEAVY_CPU" ;;
    *)
      echo "Invalid LOCAL_MODEL_TIER=$1; use LIGHT, BALANCED, QUALITY, LIGHT_CPU, MEDIUM_CPU, or HEAVY_CPU" >&2
      return 1
      ;;
  esac
}

export LOCAL_STT_MODEL_PATH="${LOCAL_STT_MODEL_PATH:-${DATA_DIR}/models/stt/vosk-model-small-en-us-0.15}"
export LOCAL_LLM_MODEL_PATH="${LOCAL_LLM_MODEL_PATH:-${DATA_DIR}/models/llm/qwen2.5-1.5b-instruct-q4_k_m.gguf}"
export LOCAL_TTS_MODEL_PATH="${LOCAL_TTS_MODEL_PATH:-${DATA_DIR}/models/tts/en_US-lessac-medium.onnx}"
export LOCAL_TTS_BACKEND="${LOCAL_TTS_BACKEND:-piper}"

MODEL_READY_MARKER="${DATA_DIR}/models/.floodman-models-ready"
if [[ -f "${MODEL_READY_MARKER}" ]]; then
  if [[ ! -d "${LOCAL_STT_MODEL_PATH}" || ! -f "${LOCAL_LLM_MODEL_PATH}" || ! -f "${LOCAL_TTS_MODEL_PATH}" || ! -f "${LOCAL_TTS_MODEL_PATH}.json" ]]; then
    echo "Local model marker is stale; required model files are missing" >&2
    rm -f "${MODEL_READY_MARKER}"
  fi
fi

if truthy "${AUTO_INSTALL_LOCAL_MODELS:-false}" && [[ ! -f "${MODEL_READY_MARKER}" ]]; then
  if [[ "${IMAGE_FLAVOR:-full}" != "full" ]]; then
    echo "AUTO_INSTALL_LOCAL_MODELS requires the full image, not the lite image" >&2
    exit 1
  fi
  RESOLVED_MODEL_TIER="$(normalize_local_model_tier "${LOCAL_MODEL_TIER:-LIGHT}")"
  "${AVA_RUNTIME_DIR}/scripts/model_setup.sh" \
    --tier "${RESOLVED_MODEL_TIER}" \
    --assume-yes \
    --language "${LOCAL_MODEL_LANGUAGE:-en-US}"
  test -d "${LOCAL_STT_MODEL_PATH}"
  test -f "${LOCAL_LLM_MODEL_PATH}"
  test -f "${LOCAL_TTS_MODEL_PATH}"
  test -f "${LOCAL_TTS_MODEL_PATH}.json"
  touch "${MODEL_READY_MARKER}"
fi

# Install or select a persistent Piper receptionist voice. Downloading
# is atomic and happens only when the chosen profile is not already
# present. Failure falls back to the existing Lessac medium voice so a
# temporary model-host outage never blocks telephone startup.
if [[ -f "${MODEL_READY_MARKER}" && "${IMAGE_FLAVOR:-full}" == "full" ]]; then
  if VOICE_PATH="$(
    /opt/venv/bin/python /opt/floodman/scripts/prepare_floodman_voice.py       --profile "${FLOODMAN_VOICE_PROFILE:-warm_female}"       --models-dir "${DATA_DIR}/models/tts"
  )"; then
    export LOCAL_TTS_MODEL_PATH="${VOICE_PATH}"
    echo "Floodman voice profile ${FLOODMAN_VOICE_PROFILE:-warm_female}: ${LOCAL_TTS_MODEL_PATH}"
  else
    echo "Floodman voice profile download failed; retaining ${LOCAL_TTS_MODEL_PATH}" >&2
  fi
fi

if [[ -f "${MODEL_READY_MARKER}" && "${IMAGE_FLAVOR:-full}" == "full" ]]; then
  export LOCAL_AI_MODE="${LOCAL_AI_MODE:-full}"
else
  export LOCAL_AI_MODE="${LOCAL_AI_MODE:-minimal}"
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
 AVA writable runtime: ${AVA_RUNTIME_DIR}
 Persistent data: ${DATA_DIR}
 Knowledge library: ${KNOWLEDGE_DIR}
============================================================
BANNER

exec /usr/bin/supervisord -c /opt/floodman/supervisord.conf
