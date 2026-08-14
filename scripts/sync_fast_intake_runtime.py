#!/usr/bin/env python3
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


MANAGED_PREFIX = "floodman_"


def load_mapping(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a YAML mapping")
    return value


def sync_managed_config(
    canonical_path: Path,
    target_path: Path,
) -> bool:
    canonical = load_mapping(canonical_path)
    target = load_mapping(target_path)

    canonical_tools = canonical.get("in_call_tools") or {}
    if not isinstance(canonical_tools, dict):
        raise TypeError("canonical in_call_tools must be a mapping")
    if "floodman_submit_intake" not in canonical_tools:
        raise RuntimeError(
            "canonical configuration is missing floodman_submit_intake"
        )

    target_tools = target.setdefault("in_call_tools", {})
    if not isinstance(target_tools, dict):
        target_tools = {}
        target["in_call_tools"] = target_tools

    before = yaml.safe_dump(
        target,
        sort_keys=False,
        allow_unicode=True,
        width=4096,
    )

    for name in list(target_tools):
        if str(name).startswith(MANAGED_PREFIX):
            target_tools.pop(name, None)
    for name, config in canonical_tools.items():
        if str(name).startswith(MANAGED_PREFIX):
            target_tools[name] = deepcopy(config)

    canonical_llm = canonical.get("llm") or {}
    if isinstance(canonical_llm, dict) and canonical_llm.get(
        "initial_greeting"
    ):
        target.setdefault("llm", {})["initial_greeting"] = str(
            canonical_llm["initial_greeting"]
        )

    canonical_no_input = canonical.get("no_input")
    if isinstance(canonical_no_input, dict):
        target["no_input"] = deepcopy(canonical_no_input)

    submit = target_tools["floodman_submit_intake"]
    if not submit.get("enabled"):
        raise RuntimeError("floodman_submit_intake must be enabled")

    for disabled_name in (
        "floodman_check_availability",
        "floodman_schedule_inspection",
        "floodman_reschedule_inspection",
    ):
        disabled = target_tools.get(disabled_name) or {}
        if disabled.get("enabled"):
            raise RuntimeError(
                f"{disabled_name} must remain disabled for fast intake"
            )

    rendered = yaml.safe_dump(
        target,
        sort_keys=False,
        allow_unicode=True,
        width=4096,
    )
    if rendered == before:
        return False

    target_path.write_text(
        rendered,
        encoding="utf-8",
        newline="\n",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--canonical",
        type=Path,
        default=(
            Path(__file__).resolve().parents[1]
            / "config/ava/ai-agent.local.yaml"
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        action="append",
        required=True,
    )
    args = parser.parse_args()

    for target in args.config:
        changed = sync_managed_config(
            args.canonical.resolve(),
            target.resolve(),
        )
        print(
            "Synchronized Floodman-managed AVA tools in "
            f"{target} (changed={str(changed).lower()})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
