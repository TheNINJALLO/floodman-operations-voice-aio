#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from agi_common import get_variable, read_agi_environment, request_json, set_variable


def main() -> int:
    env = read_agi_environment()
    try:
        source_hint = get_variable("FLOODMAN_SOURCE_HINT")
        trunk = get_variable("FLOODMAN_TRUNK")
        direction = get_variable("FLOODMAN_DIRECTION") or "inbound"
        call_id = env.get("agi_uniqueid", "")
        response = request_json(
            "POST",
            "/internal/gate/start",
            {
                "call_id": call_id,
                "caller_number": env.get("agi_callerid", ""),
                "caller_name": env.get("agi_calleridname", ""),
                "did": env.get("agi_dnid") or env.get("agi_extension", ""),
                "trunk": trunk,
                "source_hint": source_hint,
                "direction": direction,
                "metadata": {
                    "channel": env.get("agi_channel", ""),
                    "context": env.get("agi_context", ""),
                    "request": env.get("agi_request", ""),
                },
            },
        )
        set_variable("FLOODMAN_GATE_UUID", response.get("gate_uuid", ""))
        set_variable("FLOODMAN_GATE_BYPASS", "0" if response.get("gate_enabled") else "1")
        set_variable("AI_AGENT", response.get("default_agent", "floodman_inbound"))
        set_variable("AI_PROVIDER", response.get("default_provider", "local_hybrid"))
    except Exception as exc:
        set_variable("FLOODMAN_GATE_BYPASS", "1")
        set_variable("FLOODMAN_GATE_ERROR", type(exc).__name__)
        set_variable("AI_AGENT", os.getenv("DEFAULT_AGENT", "floodman_inbound"))
        set_variable("AI_PROVIDER", os.getenv("DEFAULT_PROVIDER", "local_hybrid"))

    # Register recording metadata now that gate start has run and the file
    # path is known.  Failures are silenced so the call is never dropped.
    try:
        rec_enabled = get_variable("FLOODMAN_RECORDING_ENABLED")
        rec_file = get_variable("FLOODMAN_REC_FILE")
        if rec_enabled == "1" and rec_file:
            call_id = env.get("agi_uniqueid", "")
            direction = get_variable("FLOODMAN_DIRECTION") or "inbound"
            request_json(
                "POST",
                "/internal/recordings/start",
                {
                    "asterisk_unique_id": call_id,
                    "call_id": call_id,
                    "direction": direction,
                    "caller_number": env.get("agi_callerid", ""),
                    "called_number": env.get("agi_dnid") or env.get("agi_extension", ""),
                    "source": get_variable("FLOODMAN_SOURCE_HINT") or "unknown",
                    "file_path": rec_file,
                },
            )
            set_variable("FLOODMAN_REC_REGISTERED", "1")
    except Exception:  # noqa: BLE001
        pass  # Recording failure must never affect the call

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
