#!/usr/bin/env bash
set -euo pipefail
# shellcheck disable=SC1091
source /home/container/data/runtime/voice-process.env
exec /opt/floodman/scripts/wait-for-ready.sh \
  http://127.0.0.1:8002/ready 900 \
  /usr/sbin/asterisk -f -C /home/container/data/asterisk/etc/asterisk.conf

