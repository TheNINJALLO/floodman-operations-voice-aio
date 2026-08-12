#!/usr/bin/env python3
"""Post-hangup AGI handler that finalizes the call recording.

Called from the 'h' (hangup) extension in the Asterisk dialplan.
Failures are silenced so no hangup-path error can re-enter the call flow.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from agi_common import get_variable, read_agi_environment, request_json


def main() -> int:
    env = read_agi_environment()
    try:
        rec_enabled = get_variable("FLOODMAN_RECORDING_ENABLED")
        if rec_enabled != "1":
            return 0
        call_id = env.get("agi_uniqueid", "")
        if not call_id:
            return 0
        rec_file = get_variable("FLOODMAN_REC_FILE") or ""
        protected = get_variable("FLOODMAN_PAYMENT_SEGMENT") == "1"
        transfer_included = get_variable("FLOODMAN_RECORDING_INCLUDE_TRANSFERS") == "1"
        request_json(
            "POST",
            "/internal/recordings/finalize",
            {
                "asterisk_unique_id": call_id,
                "call_id": call_id,
                "file_path": rec_file,
                "protected_segment": protected,
                "transfer_included": transfer_included,
            },
        )
    except Exception:  # noqa: BLE001
        pass  # Never raise from the hangup handler
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
