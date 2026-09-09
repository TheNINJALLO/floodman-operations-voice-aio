from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import Settings
from app.db import Database
from app.models import IntakeState

logger = logging.getLogger(__name__)


def _first_hmac_key(value: str) -> tuple[str, bytes] | None:
    for raw in value.split(","):
        entry = raw.strip()
        if not entry or ":" not in entry:
            continue
        key_id, encoded = entry.split(":", 1)
        try:
            secret = base64.b64decode(encoded, validate=True)
        except ValueError:
            continue
        if key_id and len(secret) >= 16:
            return key_id, secret
    return None


def signed_headers(key_id: str, secret: bytes, body: bytes, timestamp: int) -> dict[str, str]:
    issued_at = str(timestamp)
    digest = hmac.new(secret, issued_at.encode("ascii") + b"." + body, hashlib.sha256).digest()
    return {
        "x-floodman-key-id": key_id,
        "x-floodman-timestamp": issued_at,
        "x-floodman-signature": base64.b64encode(digest).decode("ascii"),
        "content-type": "application/json",
    }


def _caller_name(value: str) -> tuple[str, str]:
    parts = [part for part in value.strip().split() if part]
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _summary(state: IntakeState) -> str:
    parts = [
        state.description,
        f"Property: {state.property_context}" if state.property_context else "",
        f"Affected area: {state.affected_area}" if state.affected_area else "",
        f"Started: {state.timing_summary}" if state.timing_summary else "",
        f"Source/spread: {state.source_summary}" if state.source_summary else "",
        f"Safety: {state.safety_summary}" if state.safety_summary else "",
    ]
    return " | ".join(part for part in parts if part)[:5000]


def _postal_code(value: str) -> str:
    match = re.search(r"(?<!\d)(\d{5}(?:-\d{4})?)(?!\d)", value)
    return match.group(1) if match else ""


class BusinessSuiteBridge:
    """Durable, fail-open delivery from Voice AIO to the local Business Suite.

    Every event is committed to the Voice SQLite database before the background
    worker attempts loopback delivery. Call audio and caller responses never wait
    for PostgreSQL, Gauzy, Office, email, or another business process.
    """

    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.business_suite_events_enabled
            and self.settings.business_suite_events_url
            and self.settings.business_suite_hmac_keys
            and self.settings.business_suite_organization_id
            and self.settings.business_suite_workspace_id
        )

    def _payload(
        self,
        state: IntakeState,
        event_type: str,
        sequence: int,
        *,
        failure_reason: str = "",
    ) -> dict[str, Any]:
        first_name, last_name = _caller_name(state.name)
        caller_phone = state.phone or state.caller_number
        phone_verified = bool(
            caller_phone
            and state.caller_number
            and caller_phone == state.caller_number
            and self.settings.sip_mode == "twilio"
            and self.settings.sip_match_addresses
        )
        review_reasons: list[str] = []
        if event_type in {"call-ended", "call-failed"}:
            if not state.name:
                review_reasons.append("CALLER_NAME_MISSING")
            if not caller_phone:
                review_reasons.append("CALLBACK_PHONE_MISSING")
            if not state.address:
                review_reasons.append("SERVICE_ADDRESS_MISSING")
            if not state.completed:
                review_reasons.append("PARTIAL_CALL")
        if state.service_status == "unsupported":
            review_reasons.append("UNSUPPORTED_SERVICE_REVIEW")
        transcript_available = event_type != "call-started"
        transcript_reference = (
            f"{self.settings.public_base_url}/calls/{self.database.call_id(state.call_uuid)}"
            if transcript_available
            else ""
        )
        return {
            "provider": "deterministic",
            "provider_call_id": state.call_uuid,
            "event_id": f"voice-aio:{state.call_uuid}:{sequence}:{event_type}",
            "event_type": event_type,
            "event_sequence": sequence,
            "organization_id": self.settings.business_suite_organization_id,
            "workspace_id": self.settings.business_suite_workspace_id,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "caller": {
                "name": state.name,
                "first_name": first_name,
                "last_name": last_name,
                "company": "",
                "email": state.email,
                "phone": caller_phone,
                "phone_verified": phone_verified,
            },
            "property": {
                "name": "",
                "property_type": state.property_context,
                "street": state.address,
                "city": state.service_area_city,
                "state": "",
                "postal_code": _postal_code(state.address),
                "country": "US",
                "insurer": "",
                "claim_number": "",
            },
            "service_reason": state.description,
            "summary": _summary(state),
            "requested_services": [state.service_key] if state.service_key else [],
            "urgency": "EMERGENCY" if state.urgency == "emergency" else "NORMAL",
            "appointment": {
                "requested": False,
                "requested_window": "",
                "confirmed": False,
                "appointment_id": "",
            },
            "consent": {
                "sms_status": "UNKNOWN",
                "email_status": "UNKNOWN",
                "disclosure_version": "",
                "evidence_id": "",
            },
            "transcript_available": transcript_available,
            "transcript_reference": transcript_reference,
            "identity_ambiguous": False,
            "failure_reason": failure_reason[:500],
            "review_reasons": review_reasons,
        }

    def queue(self, call_id: int, state: IntakeState, event_type: str, *, failure_reason: str = "") -> None:
        if not self.enabled:
            return
        try:
            self.database.enqueue_business_event(
                call_id,
                event_type,
                lambda sequence: self._payload(state, event_type, sequence, failure_reason=failure_reason),
            )
        except Exception:
            # The voice path remains fail-open, while the local call/intake record
            # has already been committed by VoiceCore.
            logger.exception("Unable to queue Business Suite event for call %s", state.call_uuid)

    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="business-suite-event-outbox")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        key = _first_hmac_key(self.settings.business_suite_hmac_keys)
        if key is None:
            logger.error("Business Suite event delivery is enabled but its HMAC key is invalid")
            return
        key_id, secret = key
        while not self._stop.is_set():
            item = self.database.claim_business_event()
            if item is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            body = json.dumps(item["payload"], separators=(",", ":"), sort_keys=True).encode("utf-8")
            try:
                headers = signed_headers(key_id, secret, body, int(datetime.now(timezone.utc).timestamp()))
                async with httpx.AsyncClient(timeout=self.settings.business_suite_timeout_seconds) as client:
                    response = await client.post(self.settings.business_suite_events_url, content=body, headers=headers)
                if response.status_code not in {200, 202}:
                    raise RuntimeError(f"Business Suite returned HTTP {response.status_code}: {response.text[:300]}")
            except Exception as exc:
                delay = min(300.0, float(2 ** min(int(item["attempts"]), 8)))
                self.database.retry_business_event(int(item["id"]), str(exc), delay)
                logger.warning("Business Suite event %s will retry in %.0f seconds", item["event_id"], delay)
            else:
                self.database.complete_business_event(int(item["id"]))
