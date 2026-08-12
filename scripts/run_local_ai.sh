#!/usr/bin/env bash
set -euo pipefail
if [[ "${ENABLE_LOCAL_AI_SERVER:-true}" != "true" ]]; then
  exec sleep infinity
fi
cd /opt/ava
exec /opt/venv/bin/python -m local_ai_server.main
