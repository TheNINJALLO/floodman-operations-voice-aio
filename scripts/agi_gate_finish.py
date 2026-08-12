#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from agi_common import get_variable, read_agi_environment, request_json, set_variable


def main() -> int:
    read_agi_environment()
    gate_uuid = get_variable("FLOODMAN_GATE_UUID")
    if not gate_uuid:
        set_variable("FLOODMAN_GATE_BYPASS", "1")
        return 0
    try:
        decision = request_json("GET", f"/internal/gate/decision/{gate_uuid}")
        set_variable("AI_AGENT", decision.get("agent") or os.getenv("DEFAULT_AGENT", "floodman_inbound"))
        set_variable("AI_PROVIDER", decision.get("provider") or os.getenv("DEFAULT_PROVIDER", "local_hybrid"))
        set_variable("FLOODMAN_CALL_CLASS", decision.get("classification", "unknown"))
        set_variable("FLOODMAN_GATE_CONFIDENCE", decision.get("confidence", 0))
        set_variable("FLOODMAN_OPENING_TRANSCRIPT", str(decision.get("opening_transcript", ""))[:1000])
        set_variable("FLOODMAN_GOOGLE_ANNOUNCEMENT", "1" if decision.get("announcement_detected") else "0")
        set_variable("FLOODMAN_GATE_REASON", decision.get("reason", ""))
    except Exception as exc:
        set_variable("FLOODMAN_GATE_ERROR", type(exc).__name__)
        set_variable("AI_AGENT", os.getenv("DEFAULT_AGENT", "floodman_inbound"))
        set_variable("AI_PROVIDER", os.getenv("DEFAULT_PROVIDER", "local_hybrid"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
