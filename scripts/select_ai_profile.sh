#!/usr/bin/env bash
set -euo pipefail

requested="${FLOODMAN_AI_PROFILE:-auto}"
missing=()

require_cloud_keys() {
  [[ -n "${DEEPGRAM_API_KEY:-}" ]] || missing+=("DEEPGRAM_API_KEY")
  [[ -n "${GROQ_API_KEY:-}" ]] || missing+=("GROQ_API_KEY")
  [[ -n "${ELEVENLABS_API_KEY:-}" ]] || missing+=("ELEVENLABS_API_KEY")
}

case "${requested,,}" in
  auto)
    require_cloud_keys
    if [[ "${#missing[@]}" -eq 0 ]]; then
      requested="production_hybrid"
    else
      requested="local_hybrid"
      echo "Floodman production AI keys are incomplete; using local_hybrid. Missing: ${missing[*]}"
    fi
    ;;
  production|production_hybrid|floodman_production)
    requested="production_hybrid"
    require_cloud_keys
    if [[ "${#missing[@]}" -ne 0 ]]; then
      echo "FLOODMAN_AI_PROFILE=production_hybrid requires: ${missing[*]}" >&2
      return 1 2>/dev/null || exit 1
    fi
    ;;
  local|local_hybrid)
    requested="local_hybrid"
    ;;
  *)
    echo "Invalid FLOODMAN_AI_PROFILE=${requested}; use auto, production_hybrid, or local_hybrid" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

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
