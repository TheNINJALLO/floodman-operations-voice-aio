#!/usr/bin/env python3
"""Emit only safely quoted, non-secret model selection for the native launcher."""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

source_root = Path(__file__).resolve().parents[1]
if (source_root / "app").is_dir():
    sys.path.insert(0, str(source_root))

from app.model_profile import select_primary_model


def main() -> int:
    data = Path(os.getenv("DATA_DIR", "/home/container/data"))
    model = select_primary_model(Path(os.getenv("MODEL_DIR", data / "models")))
    for key, value in (("LLAMA_MODEL_PATH", model.path), ("LLAMA_MODEL_ALIAS", model.alias)):
        print(f"export {key}={shlex.quote(str(value))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
