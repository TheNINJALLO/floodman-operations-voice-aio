#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "Floodman optional HTTP tool parameters patch"


class PatchError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected one source match, found {count}")
    return text.replace(old, new, 1)


def patch_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False

    old = '''        sub = {
            "caller_number": context.caller_number or "",
            "called_number": context.called_number or "",
            "caller_name": context.caller_name or "",
            "context_name": context.context_name or "",
            "call_id": context.call_id or "",
        }
        
        # Add pre-call tool results (fetched before call started)
'''
    new = '''        sub = {
            "caller_number": context.caller_number or "",
            "called_number": context.called_number or "",
            "caller_name": context.caller_name or "",
            "context_name": context.context_name or "",
            "call_id": context.call_id or "",
        }

        # Floodman optional HTTP tool parameters patch. Every configured
        # parameter gets a substitution value before the body template is
        # rendered, even when the model omits an optional field. This prevents
        # literal placeholders such as {email} from reaching the JSON endpoint.
        for parameter in self.config.parameters:
            name = str(parameter.get("name") or "").strip()
            if not name or name in sub:
                continue
            default = parameter.get("default")
            sub[name] = "" if default is None else str(default)
        
        # Add pre-call tool results (fetched before call started)
'''
    text = replace_once(text, old, new, str(path))
    compile(text, str(path), "exec")
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ava-root", type=Path, default=Path("/opt/ava"))
    args = parser.parse_args()
    path = args.ava_root.resolve() / "src/tools/http/in_call_lookup.py"
    if not path.is_file():
        raise PatchError(f"required AVA source is missing: {path}")
    if patch_file(path):
        print(f"patched {path}")
    else:
        print("Optional HTTP tool parameter patch already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
