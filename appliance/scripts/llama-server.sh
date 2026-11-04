#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON_BIN:-${VIRTUAL_ENV:-/opt/venv}/bin/python}"
model_exports="$("${PYTHON_BIN}" "${script_dir}/primary_model.py")"
eval "${model_exports}"

case "${LOCAL_AI_DEVICE:-cpu}" in
  cpu)
    # Ignore stale GPU settings retained by the earlier appliance egg.
    gpu_layers=0
    flash_attention="${LLAMA_FLASH_ATTN:-off}"
    ;;
  cuda)
    gpu_layers="${LLAMA_GPU_LAYERS:-99}"
    flash_attention="${LLAMA_FLASH_ATTN:-on}"
    ;;
  *)
    echo "LOCAL_AI_DEVICE must be cpu or cuda" >&2
    exit 1
    ;;
esac

LLAMA_RUNTIME_DIR="${LLAMA_RUNTIME_DIR:-/opt/llama}"
cd "${LLAMA_RUNTIME_DIR}"
exec "${LLAMA_RUNTIME_DIR}/llama-server" \
  --model "${LLAMA_MODEL_PATH}" \
  --alias "${LLAMA_MODEL_ALIAS}" \
  --host 127.0.0.1 \
  --port 8081 \
  --ctx-size "${LLAMA_CONTEXT_SIZE:-4096}" \
  --n-gpu-layers "${gpu_layers}" \
  --threads "${LLAMA_THREADS:-6}" \
  --threads-batch "${LLAMA_BATCH_THREADS:-6}" \
  --batch-size "${LLAMA_BATCH_SIZE:-256}" \
  --ubatch-size "${LLAMA_UBATCH_SIZE:-128}" \
  --parallel 1 \
  --cont-batching \
  --flash-attn "${flash_attention}" \
  --jinja \
  --metrics \
  --api-key "${LLAMA_API_KEY:-floodman-local}"
