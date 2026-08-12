#!/usr/bin/env bash
set -euo pipefail
if [[ "${ASTERISK_MODE:-embedded}" != "embedded" ]]; then
  exec sleep infinity
fi
CONFIG_DIR="${ASTERISK_CONFIG_DIR:-${DATA_DIR:-/home/container/data}/asterisk/etc}"
exec /usr/sbin/asterisk -f -C "${CONFIG_DIR}/asterisk.conf" -vvv
