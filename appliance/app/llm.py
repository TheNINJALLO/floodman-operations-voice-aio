from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class LocalLLM:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(self.settings.llama_base_url.replace("/v1", "") + "/health")
            return response.status_code < 400
        except httpx.HTTPError:
            return False

    async def _chat(self, messages: list[dict[str, str]], *, max_tokens: int = 180, temperature: float = 0.1) -> str:
        payload = {
            "model": self.settings.llama_model_alias,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers = {"Authorization": f"Bearer {self.settings.llama_api_key}"}
        async with httpx.AsyncClient(timeout=self.settings.llama_timeout_seconds) as client:
            response = await client.post(f"{self.settings.llama_base_url}/chat/completions", json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        return str(data["choices"][0]["message"]["content"] or "").strip()

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
            raw = await self._chat([{"role": "system", "content": prompt}], max_tokens=100, temperature=0.0)
            return self._json_object(raw)
        except Exception as exc:
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
            return await self._chat(messages, max_tokens=120, temperature=0.1)
        except Exception as exc:
            logger.warning("Local LLM answer unavailable: %s", exc)
            return ""
