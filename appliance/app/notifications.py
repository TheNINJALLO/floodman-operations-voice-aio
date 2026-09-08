from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import httpx

from app.config import Settings
from app.db import Database
from app.models import IntakeState
from app.webpush import WebPushService

logger = logging.getLogger(__name__)


def recipients(settings: Settings, state: IntakeState) -> tuple[str, ...]:
    ordered: list[str] = list(settings.team_alert_numbers)
    if state.urgency == "emergency":
        ordered.extend(settings.emergency_alert_numbers)
    elif state.department == "billing":
        ordered.extend(settings.billing_alert_numbers)
    elif state.department == "support":
        ordered.extend(settings.support_alert_numbers)
    else:
        ordered.extend(settings.estimating_alert_numbers)
    seen: set[str] = set()
    result = []
    for value in ordered:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def build_message(state: IntakeState, callback_hours: int, *, partial: bool) -> str:
    title = "FLOODMAN PARTIAL CALL" if partial else "FLOODMAN NEW LEAD"
    service = state.service_key.replace("_", " ") or "manual review"
    lines = [
        title,
        f"Name: {state.name or 'Unknown caller'}",
        f"Phone: {state.phone or state.caller_number or 'Unavailable'}",
        f"Email: {state.email or state.email_status or 'Unavailable'}",
        f"Address: {state.address or 'Not supplied'}",
        f"Service: {service} ({state.service_status})",
        f"Service area: {state.service_area_status}{' - ' + state.service_area_city if state.service_area_city else ''}",
        f"Need: {state.description or 'No description supplied'}",
        f"Property: {state.property_context or 'Unknown'}",
        f"Started: {state.timing_summary or 'Unknown'}",
        f"Safety: {state.safety_summary or 'Unknown'}",
        f"Urgency: {state.urgency}",
        f"Call ID: {state.call_uuid}",
    ]
    if not partial:
        lines.append(f"Callback requested within {callback_hours} hours")
    return "\n".join(lines)[:1550]


class TeamNotifier:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.web_push = WebPushService(settings, database)
        self._background_tasks: set[asyncio.Task[int]] = set()

    async def call_started(self, call_id: int, state: IntakeState) -> int:
        # Never make the caller wait for an external push service before hearing
        # the greeting. The task is retained until completion and reports errors.
        task = asyncio.create_task(self.web_push.publish_call(call_id, state, "call_started"))
        self._background_tasks.add(task)
        task.add_done_callback(self._call_started_done)
        return 0

    def _call_started_done(self, task: asyncio.Task[int]) -> None:
        self._background_tasks.discard(task)
        try:
            task.result()
        except Exception:
            logger.exception("Unable to publish the incoming-call user notification")

    async def send(self, call_id: int, state: IntakeState, *, kind: str = "lead", partial: bool = False) -> int:
        values = recipients(self.settings, state)
        body = build_message(state, self.settings.callback_sla_hours, partial=partial)
        count = 0
        for recipient in values:
            key = f"{state.call_uuid}:{kind}:{recipient}"
            if self.database.notification_exists(key):
                continue
            status, response = await self._send_one(recipient, body)
            if self.database.record_notification(call_id, kind, recipient, status, response, key):
                count += 1
        try:
            count += await self.web_push.publish_call(call_id, state, kind)
        except Exception:
            logger.exception("Unable to publish the call user notification kind=%s", kind)
        return count

    async def _send_one(self, recipient: str, body: str) -> tuple[str, str]:
        if not self.settings.twilio_sms_configured:
            logger.warning("Team SMS not configured; recording dry-run for %s", recipient)
            return "not_configured", "Twilio SMS credentials or sender are missing"
        if self.settings.twilio_api_key and self.settings.twilio_api_key_secret:
            auth = (self.settings.twilio_api_key, self.settings.twilio_api_key_secret)
        else:
            auth = (self.settings.twilio_account_sid, self.settings.twilio_auth_token)
        form = {"To": recipient, "Body": body}
        if self.settings.twilio_messaging_service_sid:
            form["MessagingServiceSid"] = self.settings.twilio_messaging_service_sid
        else:
            form["From"] = self.settings.twilio_sms_from_number
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.settings.twilio_account_sid}/Messages.json"
        try:
            async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
                response = await client.post(url, data=form, auth=auth)
            text = response.text[:1000]
            return ("queued" if 200 <= response.status_code < 300 else "failed", text)
        except httpx.HTTPError as exc:
            return "failed", str(exc)
