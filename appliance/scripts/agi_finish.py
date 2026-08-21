#!/usr/bin/env python3
from __future__ import annotations
import json, os, re, sys
from pathlib import Path


def consume() -> None:
    for line in sys.stdin:
        if not line.strip():
            break


def safe(value: str) -> str:
    return re.sub(r"[^+0-9A-Za-z_.:-]", "", str(value or ""))


def main() -> int:
    consume()
    call_uuid = sys.argv[1] if len(sys.argv) > 1 else ""
    path = Path(os.getenv("RUNTIME_DIR", "/home/container/data/runtime")) / "actions" / f"{call_uuid}.json"
    payload = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        finally:
            path.unlink(missing_ok=True)
    print(f"SET VARIABLE FLOODMAN_ACTION {safe(payload.get('action', 'hangup'))}")
    print(f"SET VARIABLE FLOODMAN_TRANSFER_NUMBER {safe(payload.get('number', ''))}")
    print(f"SET VARIABLE FLOODMAN_ACTION_REASON {safe(payload.get('reason', ''))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
