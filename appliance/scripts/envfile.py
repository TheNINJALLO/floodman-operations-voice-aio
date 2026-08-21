#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import secrets
import shlex
from pathlib import Path

ASSIGNMENT = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


class EnvFileError(ValueError):
    pass


def parse_value(raw: str, path: Path, line: int) -> str:
    try:
        values = shlex.split(raw, comments=True, posix=True)
    except ValueError as exc:
        raise EnvFileError(f"{path}:{line}: malformed quoted value") from exc
    return " ".join(values) if values else ""


def parse(path: Path) -> tuple[list[str], dict[str, str], dict[str, int]]:
    if not path.exists():
        return [], {}, {}
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    last: dict[str, int] = {}
    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ASSIGNMENT.match(stripped)
        if not match:
            raise EnvFileError(f"{path}:{number}: expected KEY=value")
        key, raw = match.groups()
        values[key] = parse_value(raw, path, number)
        last[key] = number - 1
    return lines, values, last


def normalize(path: Path, ensure: dict[str, str]) -> dict[str, str]:
    lines, values, last = parse(path)
    values.update({key: value for key, value in ensure.items() if not values.get(key)})
    output: list[str] = []
    emitted: set[str] = set()
    for index, line in enumerate(lines):
        match = ASSIGNMENT.match(line.strip())
        if not match:
            output.append(line)
            continue
        key = match.group(1)
        if index != last.get(key) or key in emitted:
            continue
        output.append(f"export {key}={shlex.quote(values[key])}")
        emitted.add(key)
    missing = [key for key in values if key not in emitted]
    if missing and output and output[-1].strip():
        output.append("")
    for key in sorted(missing):
        output.append(f"export {key}={shlex.quote(values[key])}")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return values


def shell_exports(path: Path, missing_only: bool) -> str:
    _, values, _ = parse(path)
    output = []
    for key, value in values.items():
        if missing_only and os.environ.get(key, "").strip():
            continue
        output.append(f"export {key}={shlex.quote(value)}")
    return "\n".join(output)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("validate", "normalize", "shell-exports"))
    parser.add_argument("path", type=Path)
    parser.add_argument("--missing-only", action="store_true")
    args = parser.parse_args()
    if args.command == "validate":
        parse(args.path)
        print(f"runtime environment valid: {args.path}")
    elif args.command == "normalize":
        normalize(
            args.path,
            {
                "ADMIN_TOKEN": secrets.token_urlsafe(32),
                "INTERNAL_TOKEN": secrets.token_urlsafe(32),
            },
        )
        print(f"runtime environment normalized: {args.path}")
    else:
        print(shell_exports(args.path, args.missing_only))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
