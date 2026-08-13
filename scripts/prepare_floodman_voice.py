#!/usr/bin/env python3
# Download and select an approved persistent Floodman Piper voice.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    filename: str
    relative_dir: str
    sha256: str
    quality: str

    @property
    def model_url(self) -> str:
        return f"{BASE}/{self.relative_dir}/{self.filename}"

    @property
    def config_url(self) -> str:
        return f"{self.model_url}.json"

    @property
    def model_card_url(self) -> str:
        return f"{BASE}/{self.relative_dir}/MODEL_CARD"


PROFILES: dict[str, VoiceProfile] = {
    "warm_female": VoiceProfile(
        filename="en_US-hfc_female-medium.onnx",
        relative_dir="en/en_US/hfc_female/medium",
        sha256="914c473788fc1fa8b63ace1cdcdb44588f4ae523d3ab37df1536616835a140b7",
        quality="medium",
    ),
    "clear_female": VoiceProfile(
        filename="en_US-lessac-high.onnx",
        relative_dir="en/en_US/lessac/high",
        sha256="4cabf7c3a638017137f34a1516522032d4fe3f38228a843cc9b764ddcbcd9e09",
        quality="high",
    ),
    "warm_male": VoiceProfile(
        filename="en_US-ryan-high.onnx",
        relative_dir="en/en_US/ryan/high",
        sha256="b3990d7606e183ec8dbfba70a4607074f162de1a0c412e0180d1ff60bb154eca",
        quality="high",
    ),
}

ALIASES = {
    "default": "warm_female",
    "female": "warm_female",
    "hfc_female": "warm_female",
    "lessac_high": "clear_female",
    "male": "warm_male",
    "ryan_high": "warm_male",
    "lessac_medium": "current",
}


def normalize_profile(value: str) -> str:
    normalized = (value or "warm_female").strip().lower().replace("-", "_")
    normalized = ALIASES.get(normalized, normalized)
    if normalized != "current" and normalized not in PROFILES:
        choices = ", ".join(["current", *sorted(PROFILES)])
        raise ValueError(f"Unknown voice profile {value!r}; choose {choices}")
    return normalized


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_atomic(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=str(destination.parent),
    )
    temp_path = Path(temp_name)
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Floodman-Voice-AIO/1.2"},
        )
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(
            request,
            timeout=180,
        ) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def ensure_profile(profile_name: str, models_dir: Path) -> Path:
    profile_name = normalize_profile(profile_name)
    models_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(models_dir, 0o700)

    if profile_name == "current":
        current = models_dir / "en_US-lessac-medium.onnx"
        current_config = current.with_suffix(current.suffix + ".json")
        if not current.is_file() or not current_config.is_file():
            raise FileNotFoundError(
                "The current Lessac medium voice model is not installed"
            )
        return current

    spec = PROFILES[profile_name]
    model = models_dir / spec.filename
    config = model.with_suffix(model.suffix + ".json")
    card = models_dir / f"{profile_name}-MODEL_CARD"

    valid_model = (
        model.is_file()
        and model.stat().st_size > 1_000_000
        and sha256_file(model) == spec.sha256
    )
    if not valid_model:
        print(
            f"Downloading Floodman voice {profile_name} ({spec.quality})...",
            file=sys.stderr,
        )
        download_atomic(spec.model_url, model)
        actual = sha256_file(model)
        if actual != spec.sha256:
            model.unlink(missing_ok=True)
            raise RuntimeError(
                f"Voice checksum mismatch for {profile_name}: {actual}"
            )

    valid_config = False
    if config.is_file():
        try:
            parsed = json.loads(config.read_text(encoding="utf-8"))
            valid_config = (
                int(parsed.get("audio", {}).get("sample_rate", 0)) > 0
            )
        except (OSError, ValueError, TypeError):
            valid_config = False
    if not valid_config:
        download_atomic(spec.config_url, config)
        parsed = json.loads(config.read_text(encoding="utf-8"))
        if int(parsed.get("audio", {}).get("sample_rate", 0)) <= 0:
            config.unlink(missing_ok=True)
            raise RuntimeError(
                f"Invalid Piper configuration for {profile_name}"
            )

    if not card.is_file():
        try:
            download_atomic(spec.model_card_url, card)
        except Exception as exc:
            print(
                f"Warning: could not retain voice MODEL_CARD: {exc}",
                file=sys.stderr,
            )

    return model


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="warm_female")
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("/home/container/data/models/tts"),
    )
    args = parser.parse_args()
    try:
        selected = ensure_profile(args.profile, args.models_dir.resolve())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Floodman voice preparation failed: {exc}", file=sys.stderr)
        return 1
    print(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
