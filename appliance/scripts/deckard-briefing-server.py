#!/usr/bin/env python3
"""Optional second CPU model, reachable only over loopback for post-call drafts."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.model_profile import select_primary_model

if os.getenv("LOCAL_DECKARD_BRIEFINGS", "false").lower() != "true":
    raise SystemExit("The separate Deckard briefing server is opt-in")
data = Path(os.environ["DATA_DIR"])
os.environ["PRIMARY_MODEL_PROFILE"] = "deckard"
model = select_primary_model(Path(os.getenv("MODEL_DIR", data / "models")))
binary = str(Path(os.getenv("LLAMA_RUNTIME_DIR", "/opt/llama")) / "llama-server")
arguments = [binary, "--model", str(model.path), "--alias", model.alias,
             "--host", "127.0.0.1", "--port", "8082", "--n-gpu-layers", "0",
             "--ctx-size", "4096", "--threads", os.getenv("BRIEFING_THREADS", "4"),
             "--threads-batch", os.getenv("BRIEFING_THREADS", "4"), "--parallel", "1", "--jinja",
             "--api-key", os.getenv("BRIEFING_API_KEY") or os.getenv("LLAMA_API_KEY", "floodman-local")]
os.execv(binary, arguments)
