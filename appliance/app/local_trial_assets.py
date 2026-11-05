"""Immutable speech assets for the opt-in, entirely local voice trial."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import yaml


CONFIG = Path(__file__).resolve().parents[1] / "config"


def manifest() -> dict:
    return json.loads((CONFIG / "local-streaming-assets.json").read_text())


def asset_path(root: Path, name: str) -> Path:
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Trial asset must stay under the model directory")
    return path


def verify_asset(path: Path, asset: dict) -> bool:
    if not path.is_file() or path.stat().st_size != asset["size_bytes"]:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == asset["sha256"]


def ensure_speech_assets(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for asset in manifest()["assets"]:
        path = asset_path(root, asset["path"])
        if verify_asset(path, asset):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        with httpx.Client(timeout=180, follow_redirects=True, trust_env=False) as client:
            with client.stream("GET", asset["url"]) as response:
                response.raise_for_status()
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes(1024 * 1024):
                        handle.write(chunk)
        if not verify_asset(temporary, asset):
            raise ValueError("Trial asset checksum mismatch: " + asset["path"])
        temporary.replace(path)
    prepare_pocket_config(root)


def prepare_pocket_config(root: Path) -> Path:
    """Replace every upstream weight/tokenizer reference with verified local files."""
    config = yaml.safe_load((CONFIG / "pocket-english.yaml").read_text())
    weights = str(asset_path(root, "pocket/model.safetensors"))
    config["weights_path"] = weights
    config["weights_path_without_voice_cloning"] = weights
    config["flow_lm"]["lookup_table"]["tokenizer_path"] = str(asset_path(root, "pocket/tokenizer.json"))
    target = asset_path(root, "pocket/config.yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    temporary.replace(target)
    return target


def verify_speech_assets(root: Path) -> None:
    for asset in manifest()["assets"]:
        if not verify_asset(asset_path(root, asset["path"]), asset):
            raise ValueError("Missing or invalid local trial asset: " + asset["path"])


def ensure_whisper_fallback(models: Path) -> None:
    from huggingface_hub import snapshot_download
    pin = manifest()["whisper_fallback"]
    snapshot_download(repo_id=pin["repository"], revision=pin["revision"],
                      allow_patterns=["config.json", "model.bin", "tokenizer.json", "vocabulary.*"],
                      local_dir=models / "faster-whisper" / "faster-whisper-small.en")
