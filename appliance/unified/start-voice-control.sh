#!/usr/bin/env bash
set -euo pipefail

suite_hmac="${AI_CALLING_HMAC_KEYS:-}"
# shellcheck disable=SC1091
source /home/container/data/runtime/voice-process.env
export AI_CALLING_HMAC_KEYS="${suite_hmac}"
export PUBLIC_BASE_URL="${VOICE_PUBLIC_BASE_URL:-https://aicall.oninetwork.com}"
export PYTHONPATH=/opt/voice
export CONFIG_DIR=/opt/voice/config
export BUSINESS_SUITE_EVENTS_ENABLED=true
export BUSINESS_SUITE_EVENTS_URL=http://127.0.0.1:9004/webhooks/ai-calling/deterministic

exec /opt/floodman/scripts/wait-for-ready.sh \
  http://127.0.0.1:8081/health 600 \
  /opt/voice-venv/bin/uvicorn app.main:app --app-dir /opt/voice \
    --host 127.0.0.1 --port 8802 --workers 1 --no-access-log
