#!/usr/bin/env bash
set -euo pipefail

truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

if ! truthy "${AVA_ENABLED:-true}"; then
  exec sleep infinity
fi

DATA_DIR="${DATA_DIR:-/home/container/data}"
AVA_RUNTIME_DIR="${AVA_RUNTIME_DIR:-${DATA_DIR}/runtime/ava}"
AVA_READY_FILE="${DATA_DIR}/runtime/ava-stasis-ready"
AVA_APP="${AVA_STASIS_APP:-asterisk-ai-voice-agent}"
ARI_BASE_URL="http://127.0.0.1:${ARI_PORT:-8088}/ari"
mkdir -p "$(dirname "${AVA_READY_FILE}")"
rm -f "${AVA_READY_FILE}"

AVA_PID=""

cleanup() {
  rm -f "${AVA_READY_FILE}"
  if [[ -n "${AVA_PID}" ]] && kill -0 "${AVA_PID}" >/dev/null 2>&1; then
    kill "${AVA_PID}" >/dev/null 2>&1 || true
    wait "${AVA_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if [[ "${ASTERISK_MODE:-embedded}" == "embedded" ]]; then
  ARI_READY=0
  for _ in $(seq 1 "${ASTERISK_READY_TIMEOUT_SECONDS:-120}"); do
    if curl -fsS -u "${ARI_USERNAME}:${ARI_SECRET}" \
      "${ARI_BASE_URL}/asterisk/info" >/dev/null 2>&1; then
      ARI_READY=1
      break
    fi
    sleep 1
  done
  if [[ "${ARI_READY}" != "1" ]]; then
    echo "Asterisk ARI did not become ready before AVA startup" >&2
    exit 1
  fi
fi

USES_LOCAL_PIPELINE=0
case "${AVA_PIPELINE:-local_hybrid},${AVA_PROVIDER:-local_hybrid},${DEFAULT_PROVIDER:-local_hybrid}" in
  *local*) USES_LOCAL_PIPELINE=1 ;;
esac

if truthy "${ENABLE_LOCAL_AI_SERVER:-true}" && [[ "${USES_LOCAL_PIPELINE}" == "1" ]]; then
  echo "Waiting for Floodman's Vosk, Qwen, Piper, and local WebSocket..."
  /opt/venv/bin/python - <<'PY_READY'
import os
import socket
import sys
import time

host = os.getenv("LOCAL_WS_HOST", "127.0.0.1").strip() or "127.0.0.1"
port = int(os.getenv("LOCAL_WS_PORT", "8765"))
timeout = max(30, int(os.getenv("LOCAL_AI_READY_TIMEOUT_SECONDS", "600")))
deadline = time.monotonic() + timeout
last_error = ""

while time.monotonic() < deadline:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            print(f"Floodman local AI ready on {host}:{port}")
            sys.exit(0)
    except OSError as exc:
        last_error = str(exc)
        time.sleep(1.0)

print(
    f"Floodman local AI did not become ready on {host}:{port} "
    f"within {timeout}s: {last_error}",
    file=sys.stderr,
)
sys.exit(1)
PY_READY
fi

test -f "${AVA_RUNTIME_DIR}/main.py"
cd "${AVA_RUNTIME_DIR}"
/opt/venv/bin/python "${AVA_RUNTIME_DIR}/main.py" &
AVA_PID="$!"

STASIS_READY=0
CHECKS=$(( ${AVA_STASIS_READY_TIMEOUT_SECONDS:-180} * 2 ))
for _ in $(seq 1 "${CHECKS}"); do
  if ! kill -0 "${AVA_PID}" >/dev/null 2>&1; then
    if wait "${AVA_PID}"; then
      exit 0
    else
      exit $?
    fi
  fi

  if curl -fsS -u "${ARI_USERNAME}:${ARI_SECRET}" \
    "${ARI_BASE_URL}/applications/${AVA_APP}" >/dev/null 2>&1; then
    if [[ "${FLOODMAN_AI_PROFILE:-local_hybrid}" == "production_hybrid" ]] \
      && [[ ! -s "${DATA_DIR}/runtime/production-ai-validated.json" ]]; then
      echo "Ava registered, but production provider validation is missing" >&2
      sleep 1
      continue
    fi
    STASIS_READY=1
    : > "${AVA_READY_FILE}"
    chmod 600 "${AVA_READY_FILE}"
    echo "Floodman AVA Stasis application ready: ${AVA_APP}"
    break
  fi
  sleep 0.5
done

if [[ "${STASIS_READY}" != "1" ]]; then
  echo "AVA process started but Stasis application ${AVA_APP} never registered" >&2
  exit 1
fi

if wait "${AVA_PID}"; then
  STATUS=0
else
  STATUS=$?
fi
AVA_PID=""
exit "${STATUS}"
