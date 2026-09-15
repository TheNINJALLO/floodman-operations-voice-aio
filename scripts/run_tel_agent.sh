#!/usr/bin/env bash
set -euo pipefail

truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

DATA_DIR="${DATA_DIR:-/home/container/data}"
READY_FILE="${DATA_DIR}/runtime/tel-agent-ready"
rm -f "${READY_FILE}"

if ! truthy "${TEL_AGENT_ENABLED:-true}"; then
  echo "Tel-Agent disabled; AVA recovery engine remains available"
  exec sleep infinity
fi

missing=()
for name in DEEPGRAM_API_KEY GROQ_API_KEY ELEVENLABS_API_KEY ELEVENLABS_VOICE_ID; do
  if [[ -z "${!name:-}" ]]; then
    missing+=("${name}")
  fi
done
if [[ "${#missing[@]}" -ne 0 ]]; then
  echo "Tel-Agent not ready; AVA recovery engine will handle calls. Missing: ${missing[*]}" >&2
  exec sleep infinity
fi

for _ in $(seq 1 "${TEL_AGENT_CONTROL_READY_TIMEOUT_SECONDS:-120}"); do
  if curl -fsS "${FLOODMAN_INTERNAL_URL:-http://127.0.0.1:9000}/livez" >/dev/null 2>&1; then
    export PYTHONPATH="/opt/tel-agent:/opt/floodman${PYTHONPATH:+:${PYTHONPATH}}"
    exec /opt/venv/bin/python -m integrations.tel_agent.service \
      --host "${TEL_AGENT_HOST:-127.0.0.1}" \
      --port "${TEL_AGENT_PORT:-9020}" \
      --ready-file "${READY_FILE}"
  fi
  sleep 1
done

echo "Floodman control plane did not become ready before Tel-Agent startup" >&2
exit 1
