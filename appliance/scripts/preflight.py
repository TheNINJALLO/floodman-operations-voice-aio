#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from app.config import Settings


def check(condition: bool, name: str, detail: str, errors: list[str]) -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}: {detail}")
    if not condition:
        errors.append(f"{name}: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web-only", action="store_true")
    args = parser.parse_args()
    settings = Settings.from_env()
    errors: list[str] = []
    check(len(settings.admin_token) >= 24, "admin_token", "configured", errors)
    check(len(settings.internal_token) >= 24, "internal_token", "configured", errors)
    check(settings.database_path.parent.exists(), "data_directory", str(settings.data_dir), errors)
    if not args.web_only:
        check(shutil.which("nvidia-smi") is not None, "nvidia_runtime", "nvidia-smi is available", errors)
        memory = 0
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                check=True, text=True, capture_output=True,
            )
            memory = max(int(line.strip()) for line in result.stdout.splitlines() if line.strip())
        except Exception:
            pass
        check(memory >= 7000, "gpu_memory", f"{memory} MiB detected; 7000 MiB required", errors)
        check(settings.llm_model_path.is_file(), "llm_model", str(settings.llm_model_path), errors)
        check(settings.kokoro_model_path.is_file(), "kokoro_model", str(settings.kokoro_model_path), errors)
        check(settings.kokoro_voices_path.is_file(), "kokoro_voices", str(settings.kokoro_voices_path), errors)
        check(settings.sip_mode in {"twilio", "generic", "disabled"}, "sip_mode", settings.sip_mode, errors)
        if settings.sip_mode != "disabled":
            check(bool(settings.sip_server), "sip_server", "configured", errors)
    print(f"Blocking errors: {len(errors)}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
