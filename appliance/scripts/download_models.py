#!/usr/bin/env python3
from __future__ import annotations

import os
import hashlib
import shutil
import sys
from pathlib import Path

import httpx
source_root = Path(__file__).resolve().parents[1]
if (source_root / "app").is_dir():
    sys.path.insert(0, str(source_root))

from app.model_profile import PrimaryModel, select_primary_model


def verify_primary_model(path: Path, model: PrimaryModel) -> None:
    if not path.is_file():
        raise ValueError("Primary model file is missing")
    if model.size_bytes is not None and path.stat().st_size != model.size_bytes:
        raise ValueError("Primary model size does not match the pinned Deckard file")
    with path.open("rb") as handle:
        if handle.read(4) != b"GGUF":
            raise ValueError("Primary model is not a GGUF file")
        if model.sha256:
            handle.seek(0)
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(chunk)
            if digest.hexdigest() != model.sha256:
                raise ValueError("Primary model checksum does not match the pinned Deckard file")


def ensure_primary_model(model: PrimaryModel, data: Path, *, fetch=None) -> None:
    if model.path.exists():
        verify_primary_model(model.path, model)
        return
    # An existing model may be mounted read-only elsewhere. New downloads and
    # their resumable/cache files must stay in this appliance's DATA_DIR.
    if not model.path.resolve().is_relative_to(data.resolve()):
        raise ValueError("New model downloads must be stored under DATA_DIR")
    model.path.parent.mkdir(parents=True, exist_ok=True)
    if fetch is None:
        from huggingface_hub import hf_hub_download
        fetch = hf_hub_download
    downloaded = Path(fetch(repo_id=model.repository, filename=model.filename,
                            revision=model.revision, token=os.getenv("HF_TOKEN") or None,
                            local_dir=model.path.parent))
    verify_primary_model(downloaded, model)
    if downloaded != model.path:
        temporary = model.path.with_suffix(model.path.suffix + ".part")
        shutil.copyfile(downloaded, temporary)
        verify_primary_model(temporary, model)
        temporary.replace(model.path)


def download(url: str, path: Path) -> None:
    if path.exists() and path.stat().st_size > 100000:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    with httpx.stream("GET", url, follow_redirects=True, timeout=180.0) as response:
        response.raise_for_status()
        with temporary.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
    temporary.replace(path)


def main() -> int:
    from huggingface_hub import snapshot_download
    data = Path(os.getenv("DATA_DIR", "/home/container/data"))
    models = Path(os.getenv("MODEL_DIR", data / "models"))
    token = os.getenv("HF_TOKEN") or None

    primary = select_primary_model(models)
    ensure_primary_model(primary, data)
    print(f"Floodman primary model ready: {primary.alias} ({primary.profile})")

    stt_model = os.getenv("FASTER_WHISPER_MODEL", "small.en")
    snapshot_download(
        repo_id=f"Systran/faster-whisper-{stt_model}",
        local_dir=models / "faster-whisper" / f"faster-whisper-{stt_model}",
        token=token,
    )

    kokoro_dir = models / "kokoro"
    download(
        os.getenv("KOKORO_MODEL_URL", "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx"),
        Path(os.getenv("KOKORO_MODEL_PATH", kokoro_dir / "kokoro-v1.0.onnx")),
    )
    download(
        os.getenv("KOKORO_VOICES_URL", "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin"),
        Path(os.getenv("KOKORO_VOICES_PATH", kokoro_dir / "voices-v1.0.bin")),
    )
    print("Floodman local models are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
