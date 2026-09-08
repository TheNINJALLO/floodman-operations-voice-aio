#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys
from pathlib import Path


def consume() -> None:
    for line in sys.stdin:
        if not line.strip():
            break


def main() -> int:
    consume()
    call_uuid = sys.argv[1] if len(sys.argv) > 1 else ""
    caller = sys.argv[2] if len(sys.argv) > 2 else ""
    called = sys.argv[3] if len(sys.argv) > 3 else ""
    channel_id = sys.argv[4] if len(sys.argv) > 4 else ""
    sip_call_id = sys.argv[5] if len(sys.argv) > 5 else ""
    root = Path(os.getenv("RUNTIME_DIR", "/home/container/data/runtime")) / "precall"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{call_uuid}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "caller_number": caller,
                "called_number": called,
                "asterisk_channel_id": channel_id,
                "sip_call_id": sip_call_id,
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    print("SET VARIABLE FLOODMAN_PREPARED 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
