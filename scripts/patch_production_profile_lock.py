from __future__ import annotations

from pathlib import Path


MARKER = "explicit production will not fall back to local_hybrid"


class PatchError(RuntimeError):
    pass


def replace_once(
    text: str,
    old: str,
    new: str,
    label: str,
) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{label}: expected exactly one source match, found {count}"
        )
    return text.replace(old, new, 1)


def patch_selector(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False

    text = replace_once(
        text,
        'requested="${FLOODMAN_AI_PROFILE:-auto}"\n',
        (
            'requested_input="${FLOODMAN_AI_PROFILE:-auto}"\n'
            'requested="${requested_input}"\n'
            'request_mode="auto"\n'
        ),
        "requested-profile tracking",
    )

    text = replace_once(
        text,
        '''fallback_or_fail() {
  local reason="$1"
  if truthy "${FLOODMAN_PRODUCTION_STRICT:-false}"; then
    echo "Production AI strict mode blocked startup: ${reason}" >&2
    return 1 2>/dev/null || exit 1
  fi
  echo "Production AI unavailable; switching to local_hybrid: ${reason}" >&2
  requested="local_hybrid"
}
''',
        '''fallback_or_fail() {
  local reason="$1"
  if [[ "${request_mode}" == "explicit-production" ]] || \\
     truthy "${FLOODMAN_PRODUCTION_STRICT:-false}"; then
    echo "Production AI startup blocked; explicit production will not fall back to local_hybrid: ${reason}" >&2
    return 1 2>/dev/null || exit 1
  fi
  echo "Production AI unavailable in auto mode; switching to local_hybrid: ${reason}" >&2
  requested="local_hybrid"
}
''',
        "production fallback policy",
    )

    text = replace_once(
        text,
        '''  auto)
    require_cloud_values
''',
        '''  auto)
    request_mode="auto"
    require_cloud_values
''',
        "auto request mode",
    )

    text = replace_once(
        text,
        '''  production|production_hybrid|floodman_production)
    requested="production_hybrid"
''',
        '''  production|production_hybrid|floodman_production)
    request_mode="explicit-production"
    requested="production_hybrid"
''',
        "explicit production request mode",
    )

    text = replace_once(
        text,
        '''  local|local_hybrid)
    requested="local_hybrid"
''',
        '''  local|local_hybrid)
    request_mode="explicit-local"
    requested="local_hybrid"
''',
        "explicit local request mode",
    )

    text = replace_once(
        text,
        '''    if ! /opt/venv/bin/python \\
      /opt/floodman/scripts/validate_production_ai.py \\
      "${probe_args[@]}"; then
''',
        '''    validator_python="${FLOODMAN_VALIDATOR_PYTHON:-/opt/venv/bin/python}"
    validator_script="${FLOODMAN_PRODUCTION_VALIDATOR:-/opt/floodman/scripts/validate_production_ai.py}"
    if ! "${validator_python}" "${validator_script}" \\
      "${probe_args[@]}"; then
''',
        "testable production validator command",
    )

    text = replace_once(
        text,
        'export FLOODMAN_AI_PROFILE="${requested}"\n',
        (
            'export FLOODMAN_AI_PROFILE_REQUESTED="${requested_input}"\n'
            'export FLOODMAN_AI_PROFILE="${requested}"\n'
        ),
        "requested-profile export",
    )

    text = replace_once(
        text,
        '''if [[ "${requested}" == "production_hybrid" ]]; then
  export AVA_PIPELINE="floodman_production"
''',
        '''echo "Floodman AI profile requested: ${requested_input}"

if [[ "${requested}" == "production_hybrid" ]]; then
  export AVA_PIPELINE="floodman_production"
''',
        "profile-request startup diagnostic",
    )

    if MARKER not in text:
        raise PatchError("production lock marker was not installed")

    path.write_text(text, encoding="utf-8", newline="\n")
    return True
