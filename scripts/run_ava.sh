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

if [[ "${ASTERISK_MODE:-embedded}" == "embedded" ]]; then
  ARI_READY=0
  for _ in $(seq 1 "${ASTERISK_READY_TIMEOUT_SECONDS:-120}"); do
    if curl -fsS -u "${ARI_USERNAME}:${ARI_SECRET}" \
      "http://127.0.0.1:${ARI_PORT:-8088}/ari/asterisk/info" >/dev/null 2>&1; then
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
  echo "Waiting for the Floodman local AI models and WebSocket server..."
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
  sleep 1
fi

AVA_RUNTIME_DIR="${AVA_RUNTIME_DIR:-${DATA_DIR:-/home/container/data}/runtime/ava}"
test -f "${AVA_RUNTIME_DIR}/main.py"
cd "${AVA_RUNTIME_DIR}"
exec /opt/venv/bin/python "${AVA_RUNTIME_DIR}/main.py"
