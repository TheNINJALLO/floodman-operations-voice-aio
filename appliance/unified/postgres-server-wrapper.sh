#!/usr/bin/env bash
set -euo pipefail

readonly REAL_POSTGRES=/usr/lib/postgresql/14/bin/postgres

if [[ "$(id -u)" == "0" ]]; then
  exec setpriv --reuid=988 --regid=0 --clear-groups -- \
    env HOME=/home/container USER=container LOGNAME=container \
      "${REAL_POSTGRES}" "$@"
fi

exec "${REAL_POSTGRES}" "$@"
