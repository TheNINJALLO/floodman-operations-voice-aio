from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush_async

from app.config import Settings
from app.db import Database
from app.models import IntakeState

logger = logging.getLogger(__name__)


class WebPushService:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.private_key_path = settings.push_dir / "vapid-private.pem"
        self.public_key_path = settings.push_dir / "vapid-public.txt"
        self.public_key = self._ensure_keys()

    def _ensure_keys(self) -> str:
        self.settings.push_dir.mkdir(parents=True, exist_ok=True)
        if not self.private_key_path.exists():
            private_key = ec.generate_private_key(ec.SECP256R1())
            temporary = self.private_key_path.with_suffix(".tmp")
            temporary.write_bytes(
                private_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            temporary.chmod(0o600)
            temporary.replace(self.private_key_path)
        private_key = serialization.load_pem_private_key(self.private_key_path.read_bytes(), password=None)
        if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(private_key.curve, ec.SECP256R1):
            raise RuntimeError("The persisted VAPID key is not a P-256 private key")
        public_bytes = private_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
        public_key = base64.urlsafe_b64encode(public_bytes).decode("ascii").rstrip("=")
        persisted_public = self.public_key_path.read_text(encoding="utf-8").strip() if self.public_key_path.exists() else ""
        if persisted_public != public_key:
            temporary_public = self.public_key_path.with_suffix(".tmp")
            temporary_public.write_text(public_key + "\n", encoding="utf-8")
            temporary_public.replace(self.public_key_path)
        return public_key

    async def publish_call(self, call_id: int, state: IntakeState, kind: str) -> int:
        title = {
            "call_started": "Incoming Floodman call",
            "completed_intake": "New intake completed",
            "emergency": "Emergency call needs attention",
            "human_transfer": "Caller requested a person",
            "partial_hangup": "Partial call saved",
            "partial_no_input": "Caller needs follow-up",
        }.get(kind, "Floodman call update")
        body = {
            "call_started": "Ava answered a new call. Open the dashboard to follow the intake.",
            "completed_intake": "A caller completed intake. Open the secure dashboard for details.",
            "emergency": "An urgent call requires immediate team review.",
            "human_transfer": "A caller asked to speak with the Floodman team.",
            "partial_hangup": "A caller disconnected before intake finished. Recovered details were saved.",
            "partial_no_input": "A call ended without enough audio. Review the recovered details.",
        }.get(kind, "A call record was updated. Open the secure dashboard for details.")
        records = self.database.create_user_notifications(
            event_key=f"{state.call_uuid}:{kind}",
            kind=kind,
            title=title,
            body=body,
            url=f"/calls/{call_id}",
            call_id=call_id,
        )
        await self.send_records(records)
        return len(records)

    async def publish_manual(self, user_id: int, title: str, body: str, url: str = "/notifications") -> int:
        records = self.database.create_user_notifications(
            event_key=f"manual:{user_id}:{secrets.token_urlsafe(12)}",
            kind="manual",
            title=title,
            body=body,
            url=url,
            target_user_id=user_id,
        )
        await self.send_records(records)
        return len(records)

    async def send_records(self, records: list[dict[str, Any]]) -> None:
        jobs = []
        for record in records:
            for subscription in self.database.list_push_subscriptions(int(record["user_id"])):
                jobs.append(self._send_one(record, subscription))
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    async def _send_one(self, record: dict[str, Any], subscription: dict[str, Any]) -> None:
        payload = json.dumps(
            {
                "title": record["title"],
                "body": record["body"],
                "url": record["url"],
                "kind": record["kind"],
                "notification_id": record["id"],
            },
            separators=(",", ":"),
        )
        subscription_info = {
            "endpoint": subscription["endpoint"],
            "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
        }
        try:
            response = await webpush_async(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=str(self.private_key_path),
                vapid_claims={"sub": self.settings.web_push_subject},
                ttl=300,
                timeout=5,
            )
            if hasattr(response, "release"):
                response.release()
            self.database.push_succeeded(int(subscription["id"]))
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is None:
                status = getattr(getattr(exc, "response", None), "status", None)
            self.database.push_failed(int(subscription["id"]), remove=status in {404, 410})
            logger.warning("Web push delivery failed status=%s subscription_id=%s", status or "unknown", subscription["id"])
        except Exception:
            self.database.push_failed(int(subscription["id"]))
            logger.exception("Web push delivery failed subscription_id=%s", subscription["id"])
