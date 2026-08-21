#!/usr/bin/env bash
set -euo pipefail
exec /opt/llama/llama-server \
  --model "${LLAMA_MODEL_PATH:-/home/container/data/models/llm/Qwen3-4B-Q4_K_M.gguf}" \
  --model-alias "${LLAMA_MODEL_ALIAS:-floodman-qwen3-4b}" \
  --host 127.0.0.1 \
  --port 8081 \
  --ctx-size "${LLAMA_CONTEXT_SIZE:-4096}" \
  --n-gpu-layers "${LLAMA_GPU_LAYERS:-99}" \
  --threads "${LLAMA_THREADS:-6}" \
  --threads-batch "${LLAMA_BATCH_THREADS:-6}" \
  --batch-size "${LLAMA_BATCH_SIZE:-256}" \
  --ubatch-size "${LLAMA_UBATCH_SIZE:-128}" \
  --parallel 1 \
  --cont-batching \
  --flash-attn on \
  --jinja \
  --metrics \
  --api-key "${LLAMA_API_KEY:-floodman-local}"
