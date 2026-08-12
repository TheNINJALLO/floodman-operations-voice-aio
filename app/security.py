from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@dataclass(slots=True)
class SignedTokenManager:
    secret: str

    def create(self, data: dict[str, Any], ttl_seconds: int = 7 * 24 * 3600) -> str:
        payload = dict(data)
        payload.setdefault("tid", str(uuid.uuid4()))
        payload["exp"] = int(time.time()) + max(60, ttl_seconds)
        encoded = _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self.secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        return f"{encoded}.{signature}"

    def verify(self, token: str) -> dict[str, Any]:
        try:
            encoded, signature = token.split(".", 1)
        except ValueError as exc:
            raise ValueError("invalid_token_format") from exc
        expected = _b64encode(
            hmac.new(self.secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(expected, signature):
            raise ValueError("invalid_token_signature")
        try:
            payload = json.loads(_b64decode(encoded).decode("utf-8"))
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("invalid_token_payload") from exc
        if not isinstance(payload, dict):
            raise ValueError("invalid_token_payload")
        if int(payload.get("exp") or 0) < int(time.time()):
            raise ValueError("token_expired")
        return payload
