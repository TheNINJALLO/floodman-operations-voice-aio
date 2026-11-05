#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, sys
from pathlib import Path
from agi_protocol import consume_environment, set_variable


def safe(value: str) -> str:
    return re.sub(r"[^+0-9A-Za-z_.:-]", "", str(value or ""))


def main() -> int:
    consume_environment()
    call_uuid = sys.argv[1] if len(sys.argv) > 1 else ""
    path = Path(os.getenv("RUNTIME_DIR", "/home/container/data/runtime")) / "actions" / f"{call_uuid}.json"
    payload = {
        "action": "missing_action",
        "number": "",
        "reason": "no_action_file",
    }
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload.update(loaded)
    set_variable("FLOODMAN_ACTION", safe(payload.get('action', 'missing_action')))
    set_variable("FLOODMAN_TRANSFER_NUMBER", safe(payload.get('number', '')))
    set_variable("FLOODMAN_ACTION_REASON", safe(payload.get('reason', '')))
    path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
