#!/usr/bin/env python3
"""Validate repository configuration and deployment artifacts."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _validate_yaml(path: Path) -> None:
    try:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - error path only
        raise RuntimeError(f"invalid YAML in {path.relative_to(ROOT)}: {exc}") from exc


def main() -> int:
    try:
        tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        for suffix in ("*.yaml", "*.yml"):
            for path in sorted(ROOT.rglob(suffix)):
                if any(part in {".git", ".pytest_cache", "dist"} for part in path.parts):
                    continue
                _validate_yaml(path)

        egg_paths = [
            ROOT / "egg-floodman-operations-voice-aio-v1.1.1.json",
            ROOT / "pterodactyl" / "egg-floodman-operations-voice-aio.json",
        ]
        eggs = [json.loads(path.read_text(encoding="utf-8")) for path in egg_paths]
        if eggs[0] != eggs[1]:
            raise RuntimeError("Pterodactyl egg JSON files differ between root and pterodactyl/")
    except Exception as exc:
        print(f"configuration validation failed: {exc}", file=sys.stderr)
        return 1

    print("configuration validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
