#!/usr/bin/env python3
"""Download only pinned trial assets. Does not configure a carrier or send messages."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.local_trial_assets import ensure_speech_assets, ensure_whisper_fallback
from app.model_profile import select_primary_model
from download_models import configure_runtime_cache, ensure_primary_model


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--speech-only", action="store_true")
    parser.add_argument("--with-whisper", action="store_true")
    args = parser.parse_args()
    data = Path(os.environ["DATA_DIR"]).resolve()
    configure_runtime_cache(data)
    models = data / "models"
    ensure_speech_assets(models / "local-trial")
    if args.with_whisper:
        ensure_whisper_fallback(models)
    if not args.speech_only:
        os.environ["PRIMARY_MODEL_PROFILE"] = "haiku8b"
        ensure_primary_model(select_primary_model(models), data)
    print("Pinned local trial assets verified")


if __name__ == "__main__":
    main()
