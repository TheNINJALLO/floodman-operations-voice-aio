#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


GROQ_MARKER = "Floodman Groq reasoning controls patch"


class PatchError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected one source match, found {count}")
    return text.replace(old, new, 1)


def patch_groq(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if GROQ_MARKER in text:
        return False

    text = replace_once(
        text,
        '''            "max_tokens": runtime_options.get("max_tokens", self._pipeline_defaults.get("max_tokens")),
            "timeout_sec": float(runtime_options.get("timeout_sec", self._pipeline_defaults.get("timeout_sec", self._default_timeout))),
            "use_realtime": runtime_options.get("use_realtime", self._pipeline_defaults.get("use_realtime", False)),
        ''',
        '''            "max_tokens": runtime_options.get("max_tokens", self._pipeline_defaults.get("max_tokens")),
            # Floodman Groq reasoning controls patch.
            "top_p": runtime_options.get("top_p", self._pipeline_defaults.get("top_p")),
            "presence_penalty": runtime_options.get(
                "presence_penalty", self._pipeline_defaults.get("presence_penalty")
            ),
            "reasoning_effort": runtime_options.get(
                "reasoning_effort", self._pipeline_defaults.get("reasoning_effort")
            ),
            "reasoning_format": runtime_options.get(
                "reasoning_format", self._pipeline_defaults.get("reasoning_format")
            ),
            "service_tier": runtime_options.get(
                "service_tier", self._pipeline_defaults.get("service_tier")
            ),
            "timeout_sec": float(runtime_options.get("timeout_sec", self._pipeline_defaults.get("timeout_sec", self._default_timeout))),
            "use_realtime": runtime_options.get("use_realtime", self._pipeline_defaults.get("use_realtime", False)),
        ''',
        f"{path} Groq options",
    )

    text = replace_once(
        text,
        '''        if merged.get("max_tokens") is not None:
            payload["max_tokens"] = merged["max_tokens"]
        return payload''',
        '''        if merged.get("max_tokens") is not None:
            payload["max_tokens"] = merged["max_tokens"]
        for optional_key in (
            "top_p",
            "presence_penalty",
            "reasoning_effort",
            "reasoning_format",
            "service_tier",
        ):
            if merged.get(optional_key) is not None:
                payload[optional_key] = merged[optional_key]
        return payload''',
        f"{path} Groq payload",
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ava-root", type=Path, default=Path("/opt/ava"))
    args = parser.parse_args()
    path = args.ava_root.resolve() / "src/pipelines/openai.py"
    if not path.is_file():
        raise PatchError(f"required AVA source is missing: {path}")
    if patch_groq(path):
        print(f"patched {path}")
    else:
        print("Groq AVA patch already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
