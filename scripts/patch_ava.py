#!/usr/bin/env python3
"""Apply narrow, deterministic Floodman compatibility patches to pinned AVA.

The patch set provides two protections:

1. JSON-safe substitutions for AVA's pre-call and in-call HTTP request bodies.
2. Compatibility with the current Piper Python API, which writes WAV output via
   ``PiperVoice.synthesize_wav`` instead of the legacy two-argument
   ``PiperVoice.synthesize`` call.

Every transformation is exact and idempotent. The script exits non-zero when a
required source location in the pinned AVA checkout no longer matches, forcing a
review instead of silently building an unpatched image.
"""

from __future__ import annotations

import argparse
from pathlib import Path

JSON_PATCH_MARKER = "Floodman JSON body safety patch"
PIPER_PATCH_MARKER = "Floodman Piper API compatibility patch"


class PatchError(RuntimeError):
    """Raised when a required AVA source transformation cannot be applied."""


def _replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected exactly one source match, found {count}")
    return text.replace(old, new, 1)


def patch_in_call(path: Path) -> bool:
    """Patch AVA's in-call HTTP tool to JSON-escape body substitutions."""
    text = path.read_text(encoding="utf-8")
    if JSON_PATCH_MARKER in text:
        return False

    text = _replace_once(
        text,
        "body_str = self._substitute_variables(self.config.body_template, sub_context)",
        "body_str = self._substitute_json_variables(self.config.body_template, sub_context)",
        label=str(path),
    )

    method_marker = (
        "    def _substitute_variables(self, template: str, context: Dict[str, str]) -> str:\n"
    )
    json_method = '''    def _substitute_json_variables(self, template: str, context: Dict[str, str]) -> str:\n        \"\"\"Substitute values safely inside JSON string literals.\n\n        Floodman JSON body safety patch. Floodman's configured body templates\n        place every dynamic placeholder inside a quoted JSON string. Escaping\n        the string content here prevents quotes, backslashes, control\n        characters, and newlines from corrupting the request body. URL, header,\n        and query substitution continue to use the original method.\n        \"\"\"\n        result = template\n\n        for key, value in context.items():\n            normalized = \"\" if value is None else str(value)\n            escaped = json.dumps(normalized, ensure_ascii=False)[1:-1]\n            result = result.replace(f\"{{{key}}}\", escaped)\n\n        env_pattern = r'\\$\\{([A-Z_][A-Z0-9_]*)\\}'\n\n        def env_replacer(match):\n            normalized = os.environ.get(match.group(1), \"\")\n            return json.dumps(normalized, ensure_ascii=False)[1:-1]\n\n        return re.sub(env_pattern, env_replacer, result)\n\n'''
    text = _replace_once(
        text,
        method_marker,
        json_method + method_marker,
        label=f"{path} method insertion",
    )
    path.write_text(text, encoding="utf-8")
    return True


def patch_pre_call(path: Path) -> bool:
    """Patch AVA's pre-call HTTP body substitution with the same safety rule."""
    text = path.read_text(encoding="utf-8")
    if JSON_PATCH_MARKER in text:
        return False

    text = _replace_once(
        text,
        "body = self._substitute_variables(self.config.body_template, context)",
        "body = self._substitute_json_variables(self.config.body_template, context)",
        label=str(path),
    )

    method_marker = (
        "    def _substitute_variables(self, template: str, context: PreCallContext) -> str:\n"
    )
    json_method = '''    def _substitute_json_variables(self, template: str, context: PreCallContext) -> str:\n        \"\"\"Substitute pre-call values safely inside JSON string literals.\n\n        Floodman JSON body safety patch. This mirrors the in-call protection and\n        is deliberately used only for body templates.\n        \"\"\"\n        replacements = {\n            \"caller_number\": context.caller_number or \"\",\n            \"called_number\": context.called_number or \"\",\n            \"caller_name\": context.caller_name or \"\",\n            \"context_name\": context.context_name or \"\",\n            \"call_id\": context.call_id or \"\",\n            \"campaign_id\": context.campaign_id or \"\",\n            \"lead_id\": context.lead_id or \"\",\n        }\n        result = template\n        for key, value in replacements.items():\n            escaped = json.dumps(str(value), ensure_ascii=False)[1:-1]\n            result = result.replace(f\"{{{key}}}\", escaped)\n\n        env_pattern = r'\\$\\{([A-Z_][A-Z0-9_]*)\\}'\n\n        def env_replacer(match):\n            normalized = os.environ.get(match.group(1), \"\")\n            return json.dumps(normalized, ensure_ascii=False)[1:-1]\n\n        return re.sub(env_pattern, env_replacer, result)\n\n'''
    text = _replace_once(
        text,
        method_marker,
        json_method + method_marker,
        label=f"{path} method insertion",
    )
    path.write_text(text, encoding="utf-8")
    return True


def patch_piper_server(path: Path) -> bool:
    """Use Piper's current WAV-writing API while preserving legacy support."""
    text = path.read_text(encoding="utf-8")
    if PIPER_PATCH_MARKER in text:
        return False

    old_call = "                self.tts_model.synthesize(text, wav_file)\n"
    new_call = '''                # Floodman Piper API compatibility patch. Piper 1.3+ returns\n                # chunks from synthesize(), so passing wav_file to it can produce\n                # a valid but silent WAV. Prefer synthesize_wav() when available.\n                synthesize_wav = getattr(self.tts_model, \"synthesize_wav\", None)\n                if callable(synthesize_wav):\n                    synthesize_wav(text, wav_file)\n                else:\n                    self.tts_model.synthesize(text, wav_file)\n'''
    text = _replace_once(
        text,
        old_call,
        new_call,
        label=f"{path} Piper synthesis call",
    )

    old_hint = "Build with INCLUDE_PIPER=true or install piper-tts==1.2.0."
    new_hint = "Install the pinned Floodman Piper package or enable another TTS backend."
    text = _replace_once(
        text,
        old_hint,
        new_hint,
        label=f"{path} Piper installation hint",
    )

    path.write_text(text, encoding="utf-8")
    return True


def patch_ava(ava_root: Path) -> list[Path]:
    targets = [
        (ava_root / "src/tools/http/in_call_lookup.py", patch_in_call),
        (ava_root / "src/tools/http/generic_lookup.py", patch_pre_call),
    ]
    changed: list[Path] = []
    for path, patcher in targets:
        if not path.is_file():
            raise PatchError(f"required AVA source file is missing: {path}")
        if patcher(path):
            changed.append(path)

    # The small unit fixtures used by this project model only AVA's HTTP tools.
    # A real AVA checkout always has local_ai_server/, and in that case the
    # server.py compatibility patch is mandatory and fails closed if missing.
    local_ai_dir = ava_root / "local_ai_server"
    if local_ai_dir.exists():
        piper_server = local_ai_dir / "server.py"
        if not piper_server.is_file():
            raise PatchError(f"required AVA source file is missing: {piper_server}")
        if patch_piper_server(piper_server):
            changed.append(piper_server)

    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ava-root",
        type=Path,
        default=Path("/opt/ava"),
        help="root of the pinned AVA checkout",
    )
    args = parser.parse_args()

    try:
        changed = patch_ava(args.ava_root.resolve())
    except PatchError as exc:
        parser.error(str(exc))

    if changed:
        for path in changed:
            print(f"patched {path}")
    else:
        print("AVA compatibility patches already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
