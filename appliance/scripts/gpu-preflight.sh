#!/usr/bin/env bash
set -euo pipefail
case "${LOCAL_AI_DEVICE:-cpu}" in
  cpu)
    echo "Floodman local AI device: CPU; NVIDIA hardware is not required."
    exit 0
    ;;
  cuda) ;;
  *)
    echo "LOCAL_AI_DEVICE must be cpu or cuda" >&2
    exit 1
    ;;
esac
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader
memory="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -n1 | tr -d ' ')"
if [[ -z "$memory" || "$memory" -lt 7000 ]]; then
  echo "Floodman requires at least 7 GB of visible NVIDIA VRAM; detected ${memory:-0} MiB" >&2
  exit 1
fi
