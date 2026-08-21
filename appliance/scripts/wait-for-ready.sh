#!/usr/bin/env bash
set -euo pipefail
url="${1:?health URL required}"
timeout="${2:-600}"
started="$(date +%s)"
until curl -fsS "$url" >/dev/null 2>&1; do
  if (( $(date +%s) - started >= timeout )); then
    echo "Timed out waiting for $url" >&2
    exit 1
  fi
  sleep 2
done
shift 2
exec "$@"
