"""Small, non-executing dotenv reader used by deployment scripts.

The parser intentionally does not expand commands or variables. Existing process
environment values win, so a CI secret or shell export cannot be silently
replaced by a file on disk.
"""
from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Iterable

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EnvFileError(ValueError):
    """Raised when an environment file contains unsafe or malformed syntax."""


def _parse_value(raw: str, *, path: Path, line_number: int) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        try:
            parts = shlex.split(value, comments=True, posix=True)
        except ValueError as exc:
            raise EnvFileError(f"{path}:{line_number}: malformed quoted value") from exc
        if len(parts) != 1:
            raise EnvFileError(f"{path}:{line_number}: expected one quoted value")
        return parts[0]
    # Permit a trailing comment only when it is separated from the value by
    # whitespace. A literal # in a token remains part of the value.
    match = re.match(r"^(.*?)(?:\s+#.*)?$", value)
    return (match.group(1) if match else value).rstrip()


def load_env_file(path: str | Path, *, override: bool = False, required: bool = False) -> int:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        if required:
            raise EnvFileError(f"Environment file not found: {env_path}")
        return 0
    if not env_path.is_file():
        raise EnvFileError(f"Environment path is not a regular file: {env_path}")

    loaded = 0
    for line_number, raw_line in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise EnvFileError(f"{env_path}:{line_number}: expected KEY=VALUE")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not _KEY.fullmatch(key):
            raise EnvFileError(f"{env_path}:{line_number}: invalid environment variable name")
        if not override and key in os.environ:
            continue
        os.environ[key] = _parse_value(raw_value, path=env_path, line_number=line_number)
        loaded += 1
    return loaded


def load_env_files(paths: Iterable[str | Path], *, override: bool = False) -> int:
    total = 0
    for path in paths:
        total += load_env_file(path, override=override)
    return total
