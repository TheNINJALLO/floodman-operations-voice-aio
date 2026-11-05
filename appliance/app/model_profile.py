"""One model selection shared by download, startup, and both voice drivers."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PrimaryModel:
    profile: str
    repository: str
    filename: str
    alias: str
    path: Path
    revision: str | None = None
    sha256: str | None = None
    size_bytes: int | None = None


def select_primary_model(model_dir: Path) -> PrimaryModel:
    profile = os.getenv("PRIMARY_MODEL_PROFILE", "deckard").strip().lower()
    if profile in {"deckard", "haiku8b"}:
        # Bundled provenance is immutable source configuration. Older panel
        # LLAMA_MODEL_* values cannot silently select the previous Qwen model.
        manifest_name = "deckard-model.json" if profile == "deckard" else "haiku8b-model.json"
        candidates = [Path(__file__).resolve().parents[1] / "config" / manifest_name]
        if os.getenv("CONFIG_DIR"):
            candidates.append(Path(os.environ["CONFIG_DIR"]) / manifest_name)
        candidates.extend((Path("/opt/floodman/config") / manifest_name, Path("/opt/voice/config") / manifest_name))
        manifest = next((path for path in candidates if path.is_file()), None)
        if manifest is None:
            raise ValueError("Bundled primary model manifest is missing")
        data = json.loads(manifest.read_text(encoding="utf-8"))
        filename = data["filename"]
        return PrimaryModel(
            profile=profile, repository=data["repository"], filename=filename,
            alias=data["ollama_model"], path=Path(os.getenv("DECKARD_MODEL_PATH" if profile == "deckard" else "HAIKU8B_MODEL_PATH") or model_dir / "llm" / filename),
            revision=data["revision"], sha256=data["sha256"], size_bytes=data["size_bytes"],
        )
    if profile != "custom":
        raise ValueError("PRIMARY_MODEL_PROFILE must be deckard, haiku8b, or custom")
    values = {name: os.getenv(name, "").strip() for name in (
        "LLAMA_MODEL_REPO", "LLAMA_MODEL_FILENAME", "LLAMA_MODEL_ALIAS",
    )}
    if not all(values.values()):
        raise ValueError("Custom primary models require LLAMA_MODEL_REPO, LLAMA_MODEL_FILENAME, and LLAMA_MODEL_ALIAS")
    filename = values["LLAMA_MODEL_FILENAME"]
    if Path(filename).name != filename or "/" in filename or "\\" in filename:
        raise ValueError("LLAMA_MODEL_FILENAME must be a filename")
    return PrimaryModel(
        profile=profile, repository=values["LLAMA_MODEL_REPO"], filename=filename,
        alias=values["LLAMA_MODEL_ALIAS"], path=Path(os.getenv("LLAMA_MODEL_PATH") or model_dir / "llm" / filename),
        revision=os.getenv("LLAMA_MODEL_REVISION") or None,
    )
