#!/usr/bin/env bash
set -euo pipefail

readonly REAL_INITDB=/usr/lib/postgresql/14/bin/initdb

if [[ "$(id -u)" != "0" ]]; then
  exec "${REAL_INITDB}" "$@"
fi

forward=()
pwfile=""
pgdata=""
while (($#)); do
  case "$1" in
    --pwfile=*) pwfile="${1#--pwfile=}" ;;
    --pwfile)
      shift
      (($#)) || { echo "initdb wrapper received --pwfile without a value" >&2; exit 2; }
      pwfile="$1"
      ;;
    -D|--pgdata)
      option="$1"
      forward+=("${option}")
      shift
      (($#)) || { echo "initdb wrapper received ${option} without a value" >&2; exit 2; }
      pgdata="$1"
      forward+=("$1")
      ;;
    --pgdata=*)
      pgdata="${1#--pgdata=}"
      forward+=("$1")
      ;;
    *) forward+=("$1") ;;
  esac
  shift
done

[[ -n "${pwfile}" && -r "${pwfile}" ]] || {
  echo "initdb wrapper requires a readable --pwfile" >&2
  exit 2
}
[[ -n "${pgdata}" ]] || {
  echo "initdb wrapper requires -D/--pgdata" >&2
  exit 2
}

cat "${pwfile}" | setpriv --reuid=988 --regid=0 --clear-groups -- \
  env HOME=/home/container USER=container LOGNAME=container \
    "${REAL_INITDB}" "${forward[@]}" --pwfile=/dev/stdin

# The root-launched Suite appends its loopback-only configuration immediately
# after initdb returns. Permit only gid 0 to traverse and append that file.
setpriv --reuid=988 --regid=0 --clear-groups -- chmod 0750 "${pgdata}"
setpriv --reuid=988 --regid=0 --clear-groups -- chmod 0660 \
  "${pgdata}/postgresql.conf"
