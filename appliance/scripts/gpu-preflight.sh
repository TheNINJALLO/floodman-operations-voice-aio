#!/usr/bin/env bash
set -euo pipefail
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
memory="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | tr -d ' ')"
if [[ -z "$memory" || "$memory" -lt 7000 ]]; then
  echo "Floodman requires at least 7 GB of visible NVIDIA VRAM; detected ${memory:-0} MiB" >&2
  exit 1
fi
