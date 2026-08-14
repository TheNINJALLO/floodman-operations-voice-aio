#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "Floodman Flux v2 query contract patch"


class PatchError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{label}: expected one source match, found {count}"
        )
    return text.replace(old, new, 1)


def patch_flux(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False

    old_validate = (
        '        query_params = {\n'
        '            "model": "flux-general-en",\n'
        '            "language": merged.get("language", "en-US"),\n'
        '            "encoding": merged.get("encoding", "linear16"),\n'
        '            "sample_rate": merged.get("sample_rate", "16000"),\n'
        '            "channels": "1",\n'
        '        }\n'
    )
    new_validate = (
        '        model = str(merged.get("model") or "flux-general-en")\n'
        '        query_params = {\n'
        '            "model": model,\n'
        '            "encoding": merged.get("encoding", "linear16"),\n'
        '            "sample_rate": merged.get("sample_rate", "16000"),\n'
        '            "channels": "1",\n'
        '        }\n'
        '        # Floodman Flux v2 query contract patch.\n'
        '        # flux-general-en selects English through the model name and\n'
        '        # rejects the legacy language= query parameter with HTTP 400.\n'
        '        if model == "flux-general-multi":\n'
        '            language_hint = merged.get("language_hint") or merged.get("language")\n'
        '            if language_hint:\n'
        '                query_params["language_hint"] = str(language_hint).split("-", 1)[0]\n'
    )
    text = replace_once(
        text,
        old_validate,
        new_validate,
        f"{path} Flux validation query",
    )

    old_call = (
        '        query_params = {\n'
        '            "model": "flux-general-en",  # Required for Flux\n'
        '            "language": merged.get("language", "en-US"),\n'
        '            "encoding": merged.get("encoding", "linear16"),\n'
        '            "sample_rate": merged.get("sample_rate", "16000"),\n'
        '            "channels": "1",  # Mono audio (required for Flux)\n'
        '            # Turn detection parameters\n'
        '            "eot_threshold": merged.get("eot_threshold", "0.7"),\n'
        '            "eot_timeout_ms": merged.get("eot_timeout_ms", "5000"),\n'
        '        }\n'
    )
    new_call = (
        '        model = str(merged.get("model") or "flux-general-en")\n'
        '        query_params = {\n'
        '            "model": model,\n'
        '            "encoding": merged.get("encoding", "linear16"),\n'
        '            "sample_rate": merged.get("sample_rate", "16000"),\n'
        '            "channels": "1",  # Mono audio (required for Flux)\n'
        '            # Turn detection parameters\n'
        '            "eot_threshold": merged.get("eot_threshold", "0.7"),\n'
        '            "eot_timeout_ms": merged.get("eot_timeout_ms", "5000"),\n'
        '        }\n'
        '        if model == "flux-general-multi":\n'
        '            language_hint = merged.get("language_hint") or merged.get("language")\n'
        '            if language_hint:\n'
        '                query_params["language_hint"] = str(language_hint).split("-", 1)[0]\n'
    )
    text = replace_once(
        text,
        old_call,
        new_call,
        f"{path} Flux live-call query",
    )

    old_options = (
        '        merged = {\n'
        '            "base_url": runtime_options.get(\n'
        '                "base_url",\n'
        '                self._pipeline_defaults.get("base_url", self._provider_defaults.base_url),\n'
        '            ),\n'
        '            "language": runtime_options.get(\n'
        '                "language",\n'
        '                self._pipeline_defaults.get("language", self._provider_defaults.stt_language),\n'
        '            ),\n'
        '            "encoding": runtime_options.get(\n'
    )
    new_options = (
        '        merged = {\n'
        '            "base_url": runtime_options.get(\n'
        '                "base_url",\n'
        '                self._pipeline_defaults.get("base_url", self._provider_defaults.base_url),\n'
        '            ),\n'
        '            "model": runtime_options.get(\n'
        '                "model",\n'
        '                self._pipeline_defaults.get("model", "flux-general-en"),\n'
        '            ),\n'
        '            "language": runtime_options.get(\n'
        '                "language",\n'
        '                self._pipeline_defaults.get("language", self._provider_defaults.stt_language),\n'
        '            ),\n'
        '            "language_hint": runtime_options.get(\n'
        '                "language_hint",\n'
        '                self._pipeline_defaults.get("language_hint"),\n'
        '            ),\n'
        '            "encoding": runtime_options.get(\n'
    )
    text = replace_once(
        text,
        old_options,
        new_options,
        f"{path} Flux option composition",
    )

    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ava-root",
        type=Path,
        default=Path("/opt/ava"),
    )
    args = parser.parse_args()
    path = (
        args.ava_root.resolve()
        / "src/pipelines/deepgram_flux.py"
    )
    if not path.is_file():
        raise PatchError(f"required AVA source is missing: {path}")
    if patch_flux(path):
        print(f"patched {path}")
    else:
        print("Floodman Flux patch already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
