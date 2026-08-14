#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path


MARKER = "Floodman Groq 429 backoff patch"


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


HELPERS = r'''

# Floodman Groq 429 backoff patch.
def _coerce_rate_limit_delay(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    multiplier = 1.0
    if text.endswith("ms"):
        text = text[:-2].strip()
        multiplier = 0.001
    elif text.endswith("s"):
        text = text[:-1].strip()
    try:
        return max(0.0, float(text) * multiplier)
    except (TypeError, ValueError):
        return None


def _rate_limit_retry_delay(
    response: aiohttp.ClientResponse,
    body: str,
    attempt: int,
    max_wait_sec: float,
) -> float:
    candidates: list[Any] = [
        response.headers.get("Retry-After"),
        response.headers.get("X-RateLimit-Reset-Requests"),
        response.headers.get("X-RateLimit-Reset-Tokens"),
    ]
    try:
        payload = json.loads(body or "{}")
        error = payload.get("error") or {}
        if isinstance(error, dict):
            candidates.extend(
                [
                    error.get("retry_after"),
                    error.get("reset_after"),
                ]
            )
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    limit = max(0.5, float(max_wait_sec or 10.0))
    for candidate in candidates:
        delay = _coerce_rate_limit_delay(candidate)
        if delay is not None and delay > 0:
            return min(limit, max(0.25, delay + 0.15))

    fallback = 1.0 * (2 ** max(0, int(attempt)))
    return min(limit, max(0.5, fallback))
'''


def patch_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False

    class_anchor = "\nclass OpenAILLMAdapter(LLMComponent):\n"
    if text.count(class_anchor) != 1:
        raise PatchError(
            "OpenAILLMAdapter class anchor was not found exactly once"
        )
    text = text.replace(
        class_anchor,
        HELPERS.rstrip() + "\n\n" + class_anchor.lstrip("\n"),
        1,
    )

    text = replace_once(
        text,
        '''            "service_tier": runtime_options.get(
                "service_tier", self._pipeline_defaults.get("service_tier")
            ),
            "timeout_sec": float(runtime_options.get("timeout_sec", self._pipeline_defaults.get("timeout_sec", self._default_timeout))),
        ''',
        '''            "service_tier": runtime_options.get(
                "service_tier", self._pipeline_defaults.get("service_tier")
            ),
            "rate_limit_retries": int(
                runtime_options.get(
                    "rate_limit_retries",
                    self._pipeline_defaults.get("rate_limit_retries", 2),
                )
            ),
            "rate_limit_max_wait_sec": float(
                runtime_options.get(
                    "rate_limit_max_wait_sec",
                    self._pipeline_defaults.get("rate_limit_max_wait_sec", 10.0),
                )
            ),
            "timeout_sec": float(runtime_options.get("timeout_sec", self._pipeline_defaults.get("timeout_sec", self._default_timeout))),
        ''',
        "Groq rate-limit options",
    )

    text = replace_once(
        text,
        '''        retries = 1
        tools_stripped = False
        ''',
        '''        retries = max(
            1,
            int(merged.get("rate_limit_retries", 2)),
        )
        tools_stripped = False
        ''',
        "serial retry count",
    )

    text = replace_once(
        text,
        '''                        logger.error(
                            "OpenAI chat completion failed",
                            call_id=call_id,
                            status=response.status,
                            body_preview=body[:128],
                        )
                        # Some OpenAI-compatible endpoints (notably Groq) are not reliable with tool calling.
        ''',
        '''                        logger.error(
                            "OpenAI chat completion failed",
                            call_id=call_id,
                            status=response.status,
                            body_preview=body[:128],
                        )
                        if response.status == 429 and attempt < retries:
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
                        # Some OpenAI-compatible endpoints (notably Groq) are not reliable with tool calling.
        ''',
        "serial 429 wait",
    )

    text = replace_once(
        text,
        (
            '                if response.status >= 400:\n'
            '                    body = await response.text()\n'
            '                    logger.error("OpenAI streaming failed", call_id=call_id, status=response.status, body_preview=body[:128])\n'
            '                    return\n'
        ),
        '''                if response.status >= 400:
                    body = await response.text()
                    if response.status == 429:
                        retry_attempt = int(
                            (options or {}).get(
                                "_floodman_rate_limit_attempt",
                                0,
                            )
                        )
                        retry_limit = max(
                            1,
                            int(merged.get("rate_limit_retries", 2)),
                        )
                        if retry_attempt < retry_limit:
                            delay = _rate_limit_retry_delay(
                                response,
                                body,
                                retry_attempt,
                                float(
                                    merged.get(
                                        "rate_limit_max_wait_sec",
                                        10.0,
                                    )
                                ),
                            )
                            logger.warning(
                                "Groq rate limit reached; waiting before streaming retry",
                                call_id=call_id,
                                attempt=retry_attempt + 1,
                                max_attempts=retry_limit + 1,
                                delay_seconds=round(delay, 3),
                            )
                            response.release()
                            retry_options = dict(options or {})
                            retry_options[
                                "_floodman_rate_limit_attempt"
                            ] = retry_attempt + 1
                            await asyncio.sleep(delay)
                            async for token in self.generate_stream(
                                call_id,
                                transcript,
                                context,
                                retry_options,
                            ):
                                yield token
                            return
                    logger.error(
                        "OpenAI streaming failed",
                        call_id=call_id,
                        status=response.status,
                        body_preview=body[:128],
                    )
                    return
''',
        "streaming 429 wait",
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
    path = (
        args.ava_root.resolve()
        / "src/pipelines/openai.py"
    )
    if not path.is_file():
        raise PatchError(
            f"required AVA source is missing: {path}"
        )
    if patch_file(path):
        print(f"patched {path}")
    else:
        print("Groq resilience patch already applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
