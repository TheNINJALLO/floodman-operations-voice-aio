#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "Floodman Groq model fallback patch"
TURN_MARKER = "Floodman Groq turn recovery patch"


class PatchError(RuntimeError):
    pass


def replace_once(
    text: str,
    old: str,
    new: str,
    label: str,
) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(
            f"{label}: expected one source match, found {count}"
        )
    return text.replace(old, new, 1)


def patch_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    has_model_patch = MARKER in text
    has_turn_patch = TURN_MARKER in text
    if has_model_patch and has_turn_patch:
        return False
    if has_model_patch or has_turn_patch:
        raise PatchError(
            "partial Groq fallback patch state detected; "
            "rebuild from the pinned clean AVA source"
        )

    text = replace_once(
        text,
        '''            "rate_limit_max_wait_sec": float(
                runtime_options.get(
                    "rate_limit_max_wait_sec",
                    self._pipeline_defaults.get("rate_limit_max_wait_sec", 10.0),
                )
            ),
            "timeout_sec": float(runtime_options.get("timeout_sec", self._pipeline_defaults.get("timeout_sec", self._default_timeout))),
        ''',
        '''            "rate_limit_max_wait_sec": float(
                runtime_options.get(
                    "rate_limit_max_wait_sec",
                    self._pipeline_defaults.get("rate_limit_max_wait_sec", 10.0),
                )
            ),
            # Floodman Groq model fallback patch.
            "rate_limit_fallback_model": str(
                runtime_options.get(
                    "rate_limit_fallback_model",
                    self._pipeline_defaults.get(
                        "rate_limit_fallback_model",
                        "llama-3.3-70b-versatile",
                    ),
                )
                or ""
            ).strip(),
            "timeout_sec": float(runtime_options.get("timeout_sec", self._pipeline_defaults.get("timeout_sec", self._default_timeout))),
        ''',
        "Groq fallback option",
    )

    text = replace_once(
        text,
        '''        if _url_host(chat_base) == "api.groq.com" or chat_model.startswith("llama-") or "mixtral" in chat_model:
''',
        '''        # Floodman Groq turn recovery patch.
        is_groq_component = str(
            self.component_key or ""
        ).strip().lower().startswith("groq")
        if (
            not is_groq_component
            and (
                _url_host(chat_base) == "api.groq.com"
                or chat_model.startswith("llama-")
                or "mixtral" in chat_model
            )
        ):
''',
        "Groq compatibility guard",
    )

    text = replace_once(
        text,
        '''        for optional_key in (
            "top_p",
            "presence_penalty",
            "reasoning_effort",
            "reasoning_format",
            "service_tier",
        ):
            if merged.get(optional_key) is not None:
                payload[optional_key] = merged[optional_key]
        return payload''',
        '''        for optional_key in (
            "top_p",
            "presence_penalty",
            "reasoning_effort",
            "reasoning_format",
            "service_tier",
        ):
            value = merged.get(optional_key)
            if value is None:
                continue
            if optional_key in {
                "reasoning_effort",
                "reasoning_format",
            }:
                model_name = str(
                    merged.get("chat_model") or ""
                ).strip().lower()
                supports_reasoning_controls = (
                    "qwen" in model_name
                    or "gpt-oss" in model_name
                )
                if not supports_reasoning_controls:
                    continue
            payload[optional_key] = value
        return payload''',
        "model-aware reasoning payload",
    )

    text = replace_once(
        text,
        '''                        if response.status == 429 and attempt < retries:
                            delay = _rate_limit_retry_delay(
                                response,
                                body,
                                attempt,
                                float(merged.get("rate_limit_max_wait_sec", 10.0)),
                            )
                            logger.warning(
                                "Groq rate limit reached; waiting before serial retry",
                                call_id=call_id,
                                attempt=attempt + 1,
                                max_attempts=retries + 1,
                                delay_seconds=round(delay, 3),
                            )
                            await asyncio.sleep(delay)
                            continue
        ''',
        '''                        if response.status == 429:
                            fallback_model = str(
                                merged.get("rate_limit_fallback_model") or ""
                            ).strip()
                            current_model = str(
                                payload.get("model") or ""
                            ).strip()
                            if (
                                fallback_model
                                and current_model
                                and fallback_model != current_model
                            ):
                                logger.warning(
                                    "Groq primary model rate limited; switching to fallback model",
                                    call_id=call_id,
                                    primary_model=current_model,
                                    fallback_model=fallback_model,
                                )
                                payload["model"] = fallback_model
                                for incompatible_key in (
                                    "reasoning_effort",
                                    "reasoning_format",
                                ):
                                    payload.pop(incompatible_key, None)
                                continue
                            if attempt < retries:
                                delay = _rate_limit_retry_delay(
                                    response,
                                    body,
                                    attempt,
                                    float(
                                        merged.get(
                                            "rate_limit_max_wait_sec",
                                            10.0,
                                        )
                                    ),
                                )
                                logger.warning(
                                    "Groq rate limit reached; waiting before serial retry",
                                    call_id=call_id,
                                    attempt=attempt + 1,
                                    max_attempts=retries + 1,
                                    delay_seconds=round(delay, 3),
                                )
                                await asyncio.sleep(delay)
                                continue
        ''',
        "serial Groq fallback",
    )

    text = replace_once(
        text,
        '''                    if response.status == 429:
                        retry_attempt = int(
                            (options or {}).get(
                                "_floodman_rate_limit_attempt",
                                0,
                            )
                        )
        ''',
        '''                    if response.status == 429:
                        fallback_model = str(
                            merged.get("rate_limit_fallback_model") or ""
                        ).strip()
                        current_model = str(
                            merged.get("chat_model") or ""
                        ).strip()
                        fallback_used = bool(
                            (options or {}).get(
                                "_floodman_rate_limit_fallback_used",
                                False,
                            )
                        )
                        if (
                            fallback_model
                            and current_model
                            and fallback_model != current_model
                            and not fallback_used
                        ):
                            logger.warning(
                                "Groq primary streaming model rate limited; switching to fallback model",
                                call_id=call_id,
                                primary_model=current_model,
                                fallback_model=fallback_model,
                            )
                            response.release()
                            retry_options = dict(options or {})
                            retry_options["chat_model"] = fallback_model
                            retry_options.pop("reasoning_effort", None)
                            retry_options.pop("reasoning_format", None)
                            retry_options[
                                "_floodman_rate_limit_fallback_used"
                            ] = True
                            async for token in self.generate_stream(
                                call_id,
                                transcript,
                                context,
                                retry_options,
                            ):
                                yield token
                            return
                        retry_attempt = int(
                            (options or {}).get(
                                "_floodman_rate_limit_attempt",
                                0,
                            )
                        )
        ''',
        "streaming Groq fallback",
    )

    compile(text, str(path), "exec")
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

    path = args.ava_root.resolve() / "src/pipelines/openai.py"
    if not path.is_file():
        raise PatchError(f"required AVA source is missing: {path}")

    if patch_file(path):
        print(f"patched {path}")
    else:
        print("Groq model fallback and turn recovery already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
