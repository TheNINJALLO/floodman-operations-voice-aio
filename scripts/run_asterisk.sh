#!/usr/bin/env bash
set -euo pipefail
if [[ "${ASTERISK_MODE:-embedded}" != "embedded" ]]; then
  exec sleep infinity
fi
CONFIG_DIR="${ASTERISK_CONFIG_DIR:-${DATA_DIR:-/home/container/data}/asterisk/etc}"
ASTERISK_BIN="${ASTERISK_BIN:-/usr/sbin/asterisk}"
STARTUP_VERBOSITY="${ASTERISK_STARTUP_VERBOSITY:-0}"

# Floodman configurable Asterisk startup verbosity.
case "${STARTUP_VERBOSITY}" in
  0) VERBOSITY_ARGS=() ;;
  1) VERBOSITY_ARGS=(-v) ;;
  2) VERBOSITY_ARGS=(-vv) ;;
  3) VERBOSITY_ARGS=(-vvv) ;;
  *)
    echo "Invalid ASTERISK_STARTUP_VERBOSITY=${STARTUP_VERBOSITY}; use 0, 1, 2, or 3" >&2
    exit 2
    ;;
esac

exec "${ASTERISK_BIN}"   -f   -C "${CONFIG_DIR}/asterisk.conf"   "${VERBOSITY_ARGS[@]}"
