from __future__ import annotations

import json
import logging
import time
import asyncio
from typing import Any

import httpx

from app.config import Settings
from app.call_diagnostics import emit_current

logger = logging.getLogger(__name__)


class LocalLLM:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0, trust_env=False, follow_redirects=False) as client:
                response = await client.get(self.settings.llama_base_url.replace("/v1", "") + "/health")
                if response.status_code != 200:
                    return False
                response = await client.get(self.settings.llama_base_url + "/models",
                                            headers={"Authorization": f"Bearer {self.settings.llama_api_key}"})
                response.raise_for_status()
                models = response.json().get("data", [])
            return isinstance(models, list) and any(isinstance(model, dict) and model.get("id") ==
                                                    self.settings.llama_model_alias for model in models)
        except (httpx.HTTPError, ValueError, AttributeError):
            return False

    async def _chat(self, messages: list[dict[str, str]], *, max_tokens: int = 180, temperature: float = 0.1,
                    json_schema: dict[str, Any] | None = None, timeout_seconds: float | None = None) -> str:
        payload = {
            "model": self.settings.llama_model_alias,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
            "reasoning_effort": "none",
        }
        if json_schema is not None:
            # The pinned server-common.cpp reads json_schema.schema for this
            # type. Its README's flat example silently falls back to {}.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "floodman_call_briefing", "strict": True, "schema": json_schema},
            }
        headers = {"Authorization": f"Bearer {self.settings.llama_api_key}"}
        deadline = self.settings.llama_timeout_seconds if timeout_seconds is None else timeout_seconds
        async with asyncio.timeout(deadline):
            async with httpx.AsyncClient(timeout=deadline, trust_env=False, follow_redirects=False) as client:
                response = await client.post(f"{self.settings.llama_base_url}/chat/completions", json=payload, headers=headers)
                response.raise_for_status()
                data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ValueError("Local model completion was truncated")
        return str(choice["message"]["content"] or "").strip()

    @staticmethod
    def _json_object(value: str) -> dict[str, Any]:
        text = value.strip()
        if "```" in text:
            blocks = text.split("```")
            text = next((part for part in blocks if "{" in part and "}" in part), text)
            text = text.removeprefix("json").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            result = json.loads(text[start : end + 1])
            return result if isinstance(result, dict) else {}
        except json.JSONDecodeError:
            return {}

    async def extract(self, expected_field: str, transcript: str, state: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "You extract exactly one customer-intake field. Return JSON only. "
            "Do not answer the customer and do not add commentary. "
            f"Expected field: {expected_field}. "
            f"Known state: {json.dumps(state, ensure_ascii=False)[:2500]}. "
            f"Caller said: {transcript!r}. "
            "Return {\"value\": string, \"confirmation\": \"yes\"|\"no\"|\"\", \"confidence\": 0.0-1.0}."
        )
        try:
            started = time.perf_counter()
            emit_current("llm_started", reason="extraction")
            raw = await self._chat([{"role": "system", "content": prompt}], max_tokens=100, temperature=0.0)
            emit_current("llm_completed", reason="extraction", duration_ms=(time.perf_counter() - started) * 1000)
            return self._json_object(raw)
        except Exception as exc:
            emit_current("llm_fallback", level="warning", reason="extraction", error_type=type(exc).__name__)
            logger.warning("Local LLM extraction unavailable: %s", exc)
            return {}

    async def answer(self, question: str, knowledge_context: str) -> str:
        if not knowledge_context:
            return ""
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Floodman's automated phone assistant. Answer only from the approved context. "
                    "Use at most three short sentences. Do not invent prices, timing, warranties, diagnoses, addresses, emails, or insurance promises. "
                    "When the context does not support the answer, say the Floodman team must confirm it."
                ),
            },
            {"role": "user", "content": f"Approved context:\n{knowledge_context}\n\nQuestion: {question}"},
        ]
        try:
            started = time.perf_counter()
            emit_current("llm_started", reason="knowledge_answer")
            answer = await self._chat(messages, max_tokens=120, temperature=0.1)
            emit_current("llm_completed", reason="knowledge_answer", duration_ms=(time.perf_counter() - started) * 1000)
            return answer
        except Exception as exc:
            emit_current("llm_fallback", level="warning", reason="knowledge_answer", error_type=type(exc).__name__)
            logger.warning("Local LLM answer unavailable: %s", exc)
            return ""
