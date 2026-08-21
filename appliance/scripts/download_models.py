#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

import httpx
from huggingface_hub import hf_hub_download, snapshot_download


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
    data = Path(os.getenv("DATA_DIR", "/home/container/data"))
    models = Path(os.getenv("MODEL_DIR", data / "models"))
    token = os.getenv("HF_TOKEN") or None

    llm_repo = os.getenv("LLAMA_MODEL_REPO", "Qwen/Qwen3-4B-GGUF")
    llm_file = os.getenv("LLAMA_MODEL_FILENAME", "Qwen3-4B-Q4_K_M.gguf")
    llm_target = Path(os.getenv("LLAMA_MODEL_PATH", models / "llm" / llm_file))
    llm_target.parent.mkdir(parents=True, exist_ok=True)
    if not llm_target.exists():
        downloaded = Path(
            hf_hub_download(
                repo_id=llm_repo,
                filename=llm_file,
                token=token,
                local_dir=llm_target.parent,
            )
        )
        if downloaded != llm_target:
            import shutil
            temporary = llm_target.with_suffix(llm_target.suffix + ".part")
            shutil.copyfile(downloaded, temporary)
            temporary.replace(llm_target)

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
