#!/usr/bin/env bash
set -euo pipefail
export PYTHONPATH=/opt/floodman
DATA_DIR="${DATA_DIR:-/home/container/data}"
RUNTIME_ENV="${RUNTIME_ENV:-${DATA_DIR}/runtime.env}"
mkdir -p "$DATA_DIR" "$DATA_DIR/logs" "$DATA_DIR/runtime" "$DATA_DIR/models" "$DATA_DIR/cache"
touch "$RUNTIME_ENV"
chmod 600 "$RUNTIME_ENV"

# Validate the entire file before reading or changing any value.
python /opt/floodman/scripts/envfile.py validate "$RUNTIME_ENV"
python /opt/floodman/scripts/envfile.py normalize "$RUNTIME_ENV"
eval "$(python /opt/floodman/scripts/envfile.py shell-exports "$RUNTIME_ENV" --missing-only)"

# Seed approved business data once. Operator edits in data/ are preserved.
if [[ ! -d "$DATA_DIR/knowledge" ]]; then
  cp -a /opt/floodman/knowledge "$DATA_DIR/knowledge"
fi
if [[ ! -f "$DATA_DIR/service_area.yaml" ]]; then
  cp /opt/floodman/config/service_area.yaml "$DATA_DIR/service_area.yaml"
fi

/opt/floodman/scripts/gpu-preflight.sh
python /opt/floodman/scripts/download_models.py
python /opt/floodman/scripts/preflight.py
python /opt/floodman/scripts/render_asterisk.py

exec /usr/bin/supervisord -c /opt/floodman/supervisor/supervisord.conf
