from __future__ import annotations

from pathlib import Path


PROFILE_STATUS_MARKER = "Floodman persistent AI profile status"
PRODUCTION_LOCAL_GUARD_MARKER = (
    "Floodman production profile local-model guard"
)
ASTERISK_QUIET_MARKER = "Floodman configurable Asterisk startup verbosity"


class PatchError(RuntimeError):
    pass


def replace_once(
    text: str,
    old: str,
    new: str,
    label: str,
) -> str:
    if new in text and old not in text:
        return text
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{label}: expected exactly one source match, found {count}"
        )
    return text.replace(old, new, 1)


def patch_selector(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if PROFILE_STATUS_MARKER in text:
        return False

    marker_anchor = '''marker="${DATA_DIR:-/home/container/data}/runtime/production-ai-validated.json"
mkdir -p "$(dirname "${marker}")"
rm -f "${marker}"

'''
    status_block = '''marker="${DATA_DIR:-/home/container/data}/runtime/production-ai-validated.json"
profile_status="${DATA_DIR:-/home/container/data}/runtime/ai-profile-status.env"
mkdir -p "$(dirname "${marker}")"
rm -f "${marker}"

# Floodman persistent AI profile status.
write_profile_status() {
  local state="$1"
  local reason="${2:-}"
  local validation="not-passed"
  local temporary="${profile_status}.tmp"

  if [[ -s "${marker}" ]]; then
    validation="passed"
  fi
  reason="${reason//$'\\n'/ }"

  {
    printf 'STATE=%s\\n' "${state}"
    printf 'REQUESTED_PROFILE=%s\\n' "${requested_input:-}"
    printf 'SELECTED_PROFILE=%s\\n' "${requested:-}"
    printf 'AVA_PIPELINE=%s\\n' "${AVA_PIPELINE:-}"
    printf 'AVA_PROVIDER=%s\\n' "${AVA_PROVIDER:-}"
    printf 'DEFAULT_PROVIDER=%s\\n' "${DEFAULT_PROVIDER:-}"
    printf 'LOCAL_AI_ENABLED=%s\\n' "${ENABLE_LOCAL_AI_SERVER:-}"
    printf 'PRODUCTION_VALIDATION=%s\\n' "${validation}"
    printf 'REASON=%s\\n' "${reason}"
  } > "${temporary}"
  chmod 600 "${temporary}"
  mv "${temporary}" "${profile_status}"
}

'''
    text = replace_once(
        text,
        marker_anchor,
        status_block,
        "profile-status helper",
    )

    blocked_anchor = '''    echo "Production AI startup blocked; explicit production will not fall back to local_hybrid: ${reason}" >&2
    return 1 2>/dev/null || exit 1
'''
    blocked_replacement = '''    write_profile_status "blocked" "${reason}"
    echo "Production AI startup blocked; explicit production will not fall back to local_hybrid: ${reason}" >&2
    echo "Floodman AI status file: ${profile_status}" >&2
    return 1 2>/dev/null || exit 1
'''
    text = replace_once(
        text,
        blocked_anchor,
        blocked_replacement,
        "blocked profile status",
    )

    final_anchor = '''else
  export AVA_PIPELINE="local_hybrid"
  export AVA_PROVIDER="local_hybrid"
  export DEFAULT_PROVIDER="local_hybrid"
  export ENABLE_LOCAL_AI_SERVER="true"
  echo "Floodman AI profile: local_hybrid"
fi
'''
    final_replacement = '''else
  export AVA_PIPELINE="local_hybrid"
  export AVA_PROVIDER="local_hybrid"
  export DEFAULT_PROVIDER="local_hybrid"
  export ENABLE_LOCAL_AI_SERVER="true"
  echo "Floodman AI profile: local_hybrid"
fi

write_profile_status "selected" ""
echo "Floodman AI STATUS: requested=${requested_input} selected=${requested} pipeline=${AVA_PIPELINE} local_ai=${ENABLE_LOCAL_AI_SERVER}"
echo "Floodman AI status file: ${profile_status}"
'''
    text = replace_once(
        text,
        final_anchor,
        final_replacement,
        "selected profile status",
    )

    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def patch_local_ai(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if PRODUCTION_LOCAL_GUARD_MARKER in text:
        return False

    anchor = '''truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

if ! truthy "${ENABLE_LOCAL_AI_SERVER:-true}"; then
'''
    replacement = '''truthy() {
  case "${1,,}" in
    1|true|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

# Floodman production profile local-model guard.
case "${FLOODMAN_AI_PROFILE:-auto}" in
  production|production_hybrid|floodman_production)
    echo "Floodman local AI disabled because production_hybrid is selected"
    exec sleep infinity
    ;;
esac

if ! truthy "${ENABLE_LOCAL_AI_SERVER:-true}"; then
'''
    text = replace_once(
        text,
        anchor,
        replacement,
        "production local-model guard",
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def patch_asterisk_runner(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if ASTERISK_QUIET_MARKER in text:
        return False

    old = '''CONFIG_DIR="${ASTERISK_CONFIG_DIR:-${DATA_DIR:-/home/container/data}/asterisk/etc}"
exec /usr/sbin/asterisk -f -C "${CONFIG_DIR}/asterisk.conf" -vvv
'''
    new = '''CONFIG_DIR="${ASTERISK_CONFIG_DIR:-${DATA_DIR:-/home/container/data}/asterisk/etc}"
ASTERISK_BIN="${ASTERISK_BIN:-/usr/sbin/asterisk}"
STARTUP_VERBOSITY="${ASTERISK_STARTUP_VERBOSITY:-0}"

# Floodman configurable Asterisk startup verbosity.
case "${STARTUP_VERBOSITY}" in
  0) VERBOSITY_ARGS=() ;;
  1) VERBOSITY_ARGS=(-v) ;;
  2) VERBOSITY_ARGS=(-vv) ;;
  3) VERBOSITY_ARGS=(-vvv) ;;
  *)
    echo "Invalid ASTERISK_STARTUP_VERBOSITY=${STARTUP_VERBOSITY}; use 0, 1, 2, or 3" >&2
    exit 2
    ;;
esac

exec "${ASTERISK_BIN}" \
  -f \
  -C "${CONFIG_DIR}/asterisk.conf" \
  "${VERBOSITY_ARGS[@]}"
'''
    text = replace_once(
        text,
        old,
        new,
        "Asterisk quiet runner",
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def patch_example(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    marker = "ASTERISK_STARTUP_VERBOSITY="
    if marker in text:
        return False
    text = (
        text.rstrip()
        + "\n\n"
        + "# Keep the live Pterodactyl console readable. Full Asterisk logs\n"
        + "# remain under data/asterisk/logs/full.\n"
        + "ASTERISK_STARTUP_VERBOSITY=0\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def main(root: Path) -> list[Path]:
    changed: list[Path] = []
    operations = (
        (
            root / "scripts/select_ai_profile.sh",
            patch_selector,
        ),
        (
            root / "scripts/run_local_ai.sh",
            patch_local_ai,
        ),
        (
            root / "scripts/run_asterisk.sh",
            patch_asterisk_runner,
        ),
        (
            root / ".env.twilio.example",
            patch_example,
        ),
    )
    for path, operation in operations:
        if operation(path):
            changed.append(path)
    return changed
