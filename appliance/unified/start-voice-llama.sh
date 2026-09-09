#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source /home/container/data/runtime/voice-process.env
exec /opt/floodman/scripts/llama-server.sh

