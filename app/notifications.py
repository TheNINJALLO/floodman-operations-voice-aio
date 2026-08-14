from __future__ import annotations

import logging
from typing import Any

import httpx

from app.compliance.engine import normalize_phone
from app.config import Settings

logger = logging.getLogger(__name__)

_DEPARTMENT_ALIASES = {
    "estimate": "estimating",
    "estimates": "estimating",
    "sales": "estimating",
    "scheduling": "estimating",
    "water": "estimating",
    "emergency": "emergency",
    "billing": "billing",
    "invoice": "billing",
    "support": "support",
    "service": "support",
}


def normalize_department(value: str) -> str:
    raw = str(value or "estimating").strip().lower().replace("-", "_")
    return _DEPARTMENT_ALIASES.get(raw, raw if raw in {"estimating", "emergency", "billing", "support"} else "estimating")


def team_alert_recipients(settings: Settings, department: str) -> tuple[str, ...]:
    department = normalize_department(department)
    department_values = {
        "estimating": settings.estimating_alert_numbers,
        "emergency": settings.emergency_alert_numbers,
        "billing": settings.billing_alert_numbers,
        "support": settings.support_alert_numbers,
    }.get(department, ())
    ordered = (*settings.team_alert_numbers, *department_values)
    seen: set[str] = set()
    output: list[str] = []
    for value in ordered:
        phone = normalize_phone(str(value or ""))
        if phone and phone not in seen:
            seen.add(phone)
            output.append(phone)
    return tuple(output)


def build_intake_sms(data: dict[str, Any], callback_sla_hours: int) -> str:
    department = normalize_department(str(data.get("department") or "estimating"))
    name = str(data.get("name") or "Unknown caller").strip()
    phone = normalize_phone(str(data.get("phone") or data.get("caller_number") or ""))
    address = str(data.get("address") or "Address not supplied").strip()
    description = str(data.get("description") or data.get("problem") or "No description supplied").strip()
    service = str(data.get("service") or "Property service request").strip()
    urgency = str(data.get("urgency") or "normal").strip().lower()
    call_id = str(data.get("call_id") or "").strip()
    callback_line = "Immediate callback requested" if urgency == "emergency" else f"Callback requested within {max(1, int(callback_sla_hours))} hours"
    body = (
        f"FLOODMAN {department.upper()} LEAD\n"
        f"Name: {name}\n"
        f"Phone: {phone or 'Unavailable'}\n"
        f"Address: {address}\n"
        f"Service: {service}\n"
        f"Need: {description}\n"
        f"Urgency: {urgency}\n"
        f"{callback_line}"
    )
    if call_id:
        body += f"\nCall ID: {call_id}"
    return body[:1500]


class TwilioTeamNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def configured(self) -> bool:
        has_auth = bool(
            self.settings.twilio_account_sid
            and (
                (self.settings.twilio_api_key and self.settings.twilio_api_key_secret)
                or self.settings.twilio_auth_token
            )
        )
        has_sender = bool(
            self.settings.twilio_messaging_service_sid
            or self.settings.twilio_sms_from_number
        )
        return bool(self.settings.team_sms_enabled and has_auth and has_sender)

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        recipient = normalize_phone(str(payload.get("to") or ""))
        body = str(payload.get("body") or "").strip()
        if not recipient or not body:
            return {"ok": False, "error": "sms_recipient_and_body_required"}
        if not self.configured:
            return {"ok": False, "error": "twilio_team_sms_not_configured"}

        if self.settings.twilio_api_key and self.settings.twilio_api_key_secret:
            auth = (self.settings.twilio_api_key, self.settings.twilio_api_key_secret)
        else:
            auth = (self.settings.twilio_account_sid, self.settings.twilio_auth_token)

        form: dict[str, str] = {"To": recipient, "Body": body}
        if self.settings.twilio_messaging_service_sid:
            form["MessagingServiceSid"] = self.settings.twilio_messaging_service_sid
        else:
            form["From"] = self.settings.twilio_sms_from_number

        url = (
            "https://api.twilio.com/2010-04-01/Accounts/"
            f"{self.settings.twilio_account_sid}/Messages.json"
        )
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.team_sms_timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.post(url, data=form, auth=auth)
        except httpx.HTTPError as exc:
            logger.warning("Floodman team SMS request failed: %s", exc)
            return {"ok": False, "error": f"twilio_connection_failed:{exc}"}

        try:
            result = response.json()
        except ValueError:
            result = {}
        if response.status_code < 200 or response.status_code >= 300:
            error = str(result.get("message") or response.text or f"HTTP {response.status_code}")
            logger.warning("Floodman team SMS rejected for %s: %s", recipient[-4:], error[:300])
            return {
                "ok": False,
                "status_code": response.status_code,
                "error": error[:500],
            }
        return {
            "ok": True,
            "status_code": response.status_code,
            "sid": str(result.get("sid") or ""),
            "status": str(result.get("status") or "queued"),
            "to": recipient,
        }
