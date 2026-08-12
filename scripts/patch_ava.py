#!/usr/bin/env python3
"""Apply narrow, deterministic Floodman compatibility patches to pinned AVA.

AVA's generic HTTP tool templates substitute values directly into JSON strings.
A customer value containing quotes, backslashes, tabs, or newlines can therefore
produce invalid JSON. Floodman's tool templates place dynamic values inside JSON
string literals, so this patch adds JSON-string-safe substitution for request
bodies while preserving AVA's existing URL/header substitution behavior.

The patch is intentionally exact and idempotent. It exits non-zero when the
pinned upstream source no longer matches the expected structure, forcing a
review before an AVA pin is changed.
"""

from __future__ import annotations

import argparse
from pathlib import Path

PATCH_MARKER = "Floodman JSON body safety patch"


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
    if PATCH_MARKER in text:
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
    if PATCH_MARKER in text:
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
