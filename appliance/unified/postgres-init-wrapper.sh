#!/usr/bin/env bash
set -euo pipefail

readonly REAL_INITDB=/opt/floodman/postgresql14-panel/bin/initdb

if [[ "$(id -u)" != "0" ]]; then
  exec "${REAL_INITDB}" "$@"
fi

[[ -n "${P_SERVER_UUID:-}" ]] || {
  echo "Floodman refused the PostgreSQL panel adapter outside Pterodactyl." >&2
  exit 1
}
grep -Eq '^NoNewPrivs:[[:space:]]*1$' /proc/self/status || {
  echo "Floodman refused the PostgreSQL panel adapter without no_new_privs." >&2
  exit 1
}

exec env FLOODMAN_PTERODACTYL_ROOTLESS=1 "${REAL_INITDB}" "$@"
