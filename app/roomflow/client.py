from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings
from app.db import Database

logger = logging.getLogger(__name__)

_OUTBOUND_PROMPT_KEYS = {
    "estimate_id",
    "invoice_id",
    "appointment_id",
    "callback_task_id",
    "reason",
    "lost_reason",
    "service",
    "problem",
    "source",
    "urgency",
    "preferred_time",
    "campaign_name",
}


def _safe_prompt_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:1000]
    if isinstance(value, list):
        return [_safe_prompt_value(item) for item in value[:20]]
    if isinstance(value, dict):
        return {str(key)[:80]: _safe_prompt_value(item) for key, item in list(value.items())[:30]}
    return str(value)[:1000]


@dataclass(slots=True)
class RoomflowResult:
    ok: bool
    operation: str
    status_code: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    queued: bool = False
    outbox_id: str = ""

    def model_dump(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "operation": self.operation,
            "status_code": self.status_code,
            "data": self.data,
            "error": self.error,
            "queued": self.queued,
            "outbox_id": self.outbox_id,
        }


class SafeFormat(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class RoomflowClient:
    MUTATING = {
        "create_lead",
        "create_emergency_case",
        "schedule_inspection",
        "reschedule_inspection",
        "send_payment_link",
        "create_callback_task",
        "record_call_outcome",
        "record_opt_out",
        "record_security_event",
        "send_photo_upload_link",
        "verify_customer_identity",
        "record_upload",
    }

    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database

    def _spec(self, operation: str) -> dict[str, Any] | None:
        value = self.settings.roomflow_endpoints.get(operation)
        return value if isinstance(value, dict) else None

    def _idempotency_key(self, operation: str, payload: dict[str, Any], explicit: str = "") -> str:
        if explicit:
            return explicit
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return f"floodman:{operation}:{hashlib.sha256(raw.encode()).hexdigest()[:32]}"

    async def execute(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str = "",
        queue_on_failure: bool = True,
    ) -> RoomflowResult:
        spec = self._spec(operation)
        idem = self._idempotency_key(operation, payload, idempotency_key)

        if not self.settings.roomflow_enabled:
            if operation in self.MUTATING and queue_on_failure:
                outbox_id = self.database.queue_outbox(operation, payload, idem)
                return RoomflowResult(
                    ok=False,
                    operation=operation,
                    error="Roomflow integration is disabled; operation queued.",
                    queued=True,
                    outbox_id=outbox_id,
                )
            return RoomflowResult(
                ok=False,
                operation=operation,
                error="Roomflow integration is disabled.",
            )

        if not spec:
            if operation in self.MUTATING and queue_on_failure:
                outbox_id = self.database.queue_outbox(operation, payload, idem)
                return RoomflowResult(
                    ok=False,
                    operation=operation,
                    error=f"No Roomflow endpoint mapping for {operation}; operation queued.",
                    queued=True,
                    outbox_id=outbox_id,
                )
            return RoomflowResult(ok=False, operation=operation, error="endpoint_not_configured")

        method = str(spec.get("method", "POST")).upper()
        path = str(spec.get("path", "")).format_map(SafeFormat(payload))
        if not path:
            return RoomflowResult(ok=False, operation=operation, error="empty_endpoint_path")
        url = f"{self.settings.roomflow_base_url}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.roomflow_token}",
            "X-COMPANY-ID": self.settings.roomflow_company_id,
            "Idempotency-Key": idem,
            "User-Agent": "Floodman-Operations-Voice-AIO/1.1.1",
        }
        extra_headers = spec.get("headers", {})
        if isinstance(extra_headers, dict):
            for key, value in extra_headers.items():
                headers[str(key)] = str(value).format_map(SafeFormat(payload))

        request_kwargs: dict[str, Any] = {"headers": headers}
        if method in {"GET", "DELETE"}:
            query_keys = spec.get("query_keys")
            if isinstance(query_keys, list):
                request_kwargs["params"] = {key: payload.get(key) for key in query_keys if key in payload}
            else:
                request_kwargs["params"] = payload
        else:
            body = self._shape_body(spec, payload)
            request_kwargs["json"] = body

        try:
            async with httpx.AsyncClient(
                timeout=self.settings.roomflow_timeout_seconds,
                verify=self.settings.roomflow_verify_tls,
            ) as client:
                response = await client.request(method, url, **request_kwargs)
            response.raise_for_status()
            if response.content:
                try:
                    data = response.json()
                    if not isinstance(data, dict):
                        data = {"result": data}
                except ValueError:
                    data = {"text": response.text}
            else:
                data = {}
            return RoomflowResult(
                ok=True,
                operation=operation,
                status_code=response.status_code,
                data=data,
            )
        except Exception as exc:
            logger.warning("Roomflow operation %s failed: %s", operation, exc)
            if operation in self.MUTATING and queue_on_failure:
                outbox_id = self.database.queue_outbox(operation, payload, idem)
                return RoomflowResult(
                    ok=False,
                    operation=operation,
                    error=str(exc),
                    queued=True,
                    outbox_id=outbox_id,
                )
            return RoomflowResult(ok=False, operation=operation, error=str(exc))

    @staticmethod
    def _shape_body(spec: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        body_map = spec.get("body_map")
        if not isinstance(body_map, dict):
            return payload
        result: dict[str, Any] = {}
        for target, source in body_map.items():
            if isinstance(source, str):
                value: Any = payload
                for part in source.split("."):
                    if not isinstance(value, dict) or part not in value:
                        value = None
                        break
                    value = value[part]
                result[str(target)] = value
            else:
                result[str(target)] = source
        return result

    async def lookup_customer(self, phone: str) -> RoomflowResult:
        return await self.execute("lookup_customer", {"phone": phone}, queue_on_failure=False)

    async def pre_call_context(
        self,
        call_id: str,
        phone: str,
        direction: str = "inbound",
        campaign_id: str = "",
        lead_id: str = "",
    ) -> RoomflowResult:
        local = self.database.get_gate_by_call_id(call_id) or (
            self.database.get_recent_gate_for_phone(phone) if phone and direction == "inbound" else None
        ) or {}
        outbound_job = self.database.get_outbound_job(lead_id) if lead_id else None
        safe_outbound_job: dict[str, Any] = {}
        if outbound_job:
            direction = "outbound"
            campaign_id = str(outbound_job.get("campaign_id") or campaign_id)
            # Do not inject consent evidence, authentication artifacts, or internal
            # retry details into the language-model prompt. Only operational context
            # needed to conduct this specific call is exposed.
            raw_payload = outbound_job.get("payload") or {}
            safe_payload = (
                {key: _safe_prompt_value(raw_payload.get(key)) for key in _OUTBOUND_PROMPT_KEYS if key in raw_payload}
                if isinstance(raw_payload, dict)
                else {}
            )
            safe_outbound_job = {
                "id": outbound_job.get("id"),
                "campaign_id": outbound_job.get("campaign_id"),
                "customer_id": outbound_job.get("customer_id"),
                "purpose": outbound_job.get("purpose"),
                "agent": outbound_job.get("agent"),
                "payload": safe_payload,
            }
        customer = await self.lookup_customer(phone) if phone else RoomflowResult(False, "lookup_customer")
        local_customers = self.database.search_customers(phone=phone, limit=10) if phone else []
        customer_found = bool(customer.data if customer.ok else {}) or bool(local_customers)
        # Pre-call enrichment deliberately reveals only match existence. Detailed
        # customer and account records remain behind the explicit lookup and
        # identity-verification tools so spoofed caller ID cannot trigger disclosure.
        customer_payload: dict[str, Any] = {
            "matched": customer_found,
            "local_match_count": len(local_customers),
            "roomflow_match": bool(customer.data if customer.ok else {}),
        }
        data = {
            "call_id": call_id,
            "direction": direction,
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "call_purpose": str((outbound_job or {}).get("purpose") or ""),
            "outbound_job": safe_outbound_job,
            "gate_classification": local.get("classification") or "unknown",
            "gate_confidence": local.get("confidence") or 0,
            "opening_transcript": local.get("opening_transcript") or "",
            "source_hint": local.get("source_hint") or "",
            "announcement_detected": bool(local.get("announcement_detected")),
            "customer_found": customer_found,
            "customer": customer_payload,
            "local_customers": [],
            "customer_lookup_error": customer.error if not customer.ok else "",
        }
        return RoomflowResult(ok=True, operation="pre_call_context", data=data)

    async def replay_outbox_item(self, item: dict[str, Any]) -> RoomflowResult:
        return await self.execute(
            str(item["operation"]),
            dict(item.get("payload") or {}),
            idempotency_key=str(item["idempotency_key"]),
            queue_on_failure=False,
        )
