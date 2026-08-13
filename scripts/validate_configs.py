#!/usr/bin/env python3
"""Validate repository configuration, knowledge, and deployment artifacts."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.knowledge import KnowledgeBase  # noqa: E402


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

        config = yaml.safe_load((ROOT / "config/floodman.yaml").read_text(encoding="utf-8")) or {}
        business = config.get("business", {})
        services = business.get("services", {}) if isinstance(business, dict) else {}
        required_services = {
            "water_damage_restoration",
            "mold_remediation",
            "foundation_repair",
            "basement_waterproofing",
            "crawl_space_encapsulation",
            "sump_pump_and_drainage",
        }
        missing_services = required_services - set(services)
        if missing_services:
            raise RuntimeError(f"Floodman business config is missing services: {sorted(missing_services)}")
        cities = business.get("service_area", {}).get("cities", [])
        if not isinstance(cities, list) or len({str(value).strip().lower() for value in cities}) < 100:
            raise RuntimeError("Floodman service area must contain the reviewed published community list")

        knowledge = KnowledgeBase(ROOT / "knowledge", require_approved=True)
        status = knowledge.status()
        if status["approved_documents"] < 9:
            raise RuntimeError("Knowledge library must contain at least nine approved documents")
        if status["errors"]:
            raise RuntimeError(f"Knowledge library parse errors: {status['errors']}")
        probe = knowledge.search("What does Floodman do for a burst pipe and hidden moisture?")
        if not probe["found"] or not any(
            result["category"] == "water_damage" for result in probe["results"]
        ):
            raise RuntimeError("Knowledge search failed the water-damage retrieval probe")
    except Exception as exc:
        print(f"configuration validation failed: {exc}", file=sys.stderr)
        return 1

    print("configuration validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
