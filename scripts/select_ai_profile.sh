#!/usr/bin/env bash
set -euo pipefail

truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

requested="${FLOODMAN_AI_PROFILE:-auto}"
marker="${DATA_DIR:-/home/container/data}/runtime/production-ai-validated.json"
mkdir -p "$(dirname "${marker}")"
rm -f "${marker}"

missing=()
require_cloud_values() {
  missing=()
  [[ -n "${DEEPGRAM_API_KEY:-}" ]] || missing+=("DEEPGRAM_API_KEY")
  [[ -n "${GROQ_API_KEY:-}" ]] || missing+=("GROQ_API_KEY")
  [[ -n "${ELEVENLABS_API_KEY:-}" ]] || missing+=("ELEVENLABS_API_KEY")
  [[ -n "${ELEVENLABS_VOICE_ID:-}" ]] || missing+=("ELEVENLABS_VOICE_ID")
}

fallback_or_fail() {
  local reason="$1"
  if truthy "${FLOODMAN_PRODUCTION_STRICT:-false}"; then
    echo "Production AI strict mode blocked startup: ${reason}" >&2
    return 1 2>/dev/null || exit 1
  fi
  echo "Production AI unavailable; switching to local_hybrid: ${reason}" >&2
  requested="local_hybrid"
}

case "${requested,,}" in
  auto)
    require_cloud_values
    if [[ "${#missing[@]}" -eq 0 ]]; then
      requested="production_hybrid"
    else
      requested="local_hybrid"
      echo "Floodman cloud configuration incomplete; using local_hybrid. Missing: ${missing[*]}"
    fi
    ;;
  production|production_hybrid|floodman_production)
    requested="production_hybrid"
    ;;
  local|local_hybrid)
    requested="local_hybrid"
    ;;
  *)
    echo "Invalid FLOODMAN_AI_PROFILE=${requested}; use auto, production_hybrid, or local_hybrid" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

if [[ "${requested}" == "production_hybrid" ]]; then
  require_cloud_values
  if [[ "${#missing[@]}" -ne 0 ]]; then
    fallback_or_fail "missing ${missing[*]}"
  else
    probe_args=(--marker "${marker}")
    if ! truthy "${FLOODMAN_CLOUD_AUDIO_PROBE:-true}"; then
      probe_args+=(--no-audio-probe)
    fi
    if ! /opt/venv/bin/python \
      /opt/floodman/scripts/validate_production_ai.py \
      "${probe_args[@]}"; then
      fallback_or_fail "one or more provider checks failed"
    fi
  fi
fi

export FLOODMAN_AI_PROFILE="${requested}"

if [[ "${requested}" == "production_hybrid" ]]; then
  export AVA_PIPELINE="floodman_production"
  export AVA_PROVIDER="floodman_production"
  export DEFAULT_PROVIDER="floodman_production"
  export ENABLE_LOCAL_AI_SERVER="${FLOODMAN_KEEP_LOCAL_FALLBACK_WARM:-false}"
  export AUTO_INSTALL_LOCAL_MODELS="false"
  export LOCAL_ENABLE_FILLER_AUDIO="false"
  echo "Floodman AI profile: production_hybrid"
  echo "  Listening: Deepgram Flux"
  echo "  Reasoning: Groq-hosted Qwen"
  echo "  Speaking: ElevenLabs Flash v2.5"
else
  export AVA_PIPELINE="local_hybrid"
  export AVA_PROVIDER="local_hybrid"
  export DEFAULT_PROVIDER="local_hybrid"
  export ENABLE_LOCAL_AI_SERVER="true"
  echo "Floodman AI profile: local_hybrid"
fi
