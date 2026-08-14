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
    status = str(data.get("status") or "collecting").strip().lower()
    service_status = str(data.get("service_status") or "unknown").strip().lower()
    urgency = str(data.get("urgency") or "normal").strip().lower()
    name = str(data.get("name") or "Unknown caller").strip()
    phone = normalize_phone(str(data.get("phone") or data.get("caller_number") or ""))
    email = str(data.get("email") or "").strip()
    email_status = str(data.get("email_status") or "unknown").strip()
    address = str(data.get("address") or "Address not supplied").strip()
    requested_service = str(
        data.get("service_requested")
        or data.get("service")
        or "Service not identified"
    ).strip()
    description = str(
        data.get("description")
        or data.get("problem")
        or "No description supplied"
    ).strip()
    call_id = str(data.get("call_id") or "").strip()

    if status == "unsupported" or service_status == "unsupported":
        title = "FLOODMAN OUT-OF-SCOPE REQUEST"
    elif status == "review" or service_status == "review":
        title = "FLOODMAN SERVICE REVIEW"
    elif status.startswith("partial_"):
        title = "FLOODMAN PARTIAL CALL RECOVERY"
    elif urgency == "emergency":
        title = "FLOODMAN EMERGENCY INTAKE"
    else:
        title = f"FLOODMAN {department.upper()} INTAKE"

    callback_line = (
        "Immediate callback requested"
        if urgency == "emergency"
        else f"Callback requested within {max(1, int(callback_sla_hours))} hours"
    )
    lines = [
        title,
        f"Status: {status}",
        f"Name: {name}",
        f"Phone: {phone or 'Unavailable'}",
        f"Email: {email or email_status or 'Unavailable'}",
        f"Address: {address}",
        f"Requested service: {requested_service}",
        f"Service review: {service_status}",
        f"Issue: {description}",
    ]
    optional = (
        ("Property", data.get("property_context")),
        ("Safety", data.get("safety_summary")),
        ("Timing", data.get("timing_summary")),
        ("Insurance", data.get("insurance_summary")),
        ("Photos/source", data.get("evidence_summary")),
        ("Urgency", urgency),
    )
    for label, value in optional:
        text = str(value or "").strip()
        if text:
            lines.append(f"{label}: {text}")
    if status.startswith("partial_"):
        transcript = str(data.get("transcript_text") or "").strip()
        if transcript:
            excerpt = " ".join(transcript.split())
            lines.append(f"Transcript excerpt: {excerpt[:420]}")
    lines.append(callback_line)
    if call_id:
        lines.append(f"Call ID: {call_id}")
    lines.append("Full transcript and recovered details are in the Voice AIO web app.")
    return "\n".join(lines)[:1500]



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
