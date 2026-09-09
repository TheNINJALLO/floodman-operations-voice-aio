from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import httpx

from app.config import Settings
from app.db import Database
from app.emailer import EmailService
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
        f"Affected area: {state.affected_area or 'Unknown'}",
        f"Started: {state.timing_summary or 'Unknown'}",
        f"Source/spread: {state.source_summary or 'Unknown'}",
        f"Safety: {state.safety_summary or 'Unknown'}",
        f"Urgency: {state.urgency}",
        f"Call ID: {state.call_uuid}",
    ]
    if not partial:
        lines.append(f"Callback requested within {callback_hours} hours")
    return "\n".join(lines)[:1550]


def email_subject(kind: str) -> str:
    return {
        "call_started": "Incoming Floodman call",
        "completed_intake": "New Floodman intake completed",
        "emergency": "Emergency Floodman call needs attention",
        "human_transfer": "Floodman caller requested a person",
        "partial_hangup": "Partial Floodman call needs follow-up",
        "partial_no_input": "Floodman caller needs follow-up",
    }.get(kind, "Floodman call update")


def build_email_message(
    state: IntakeState,
    callback_hours: int,
    public_base_url: str,
    call_id: int,
    *,
    kind: str,
    partial: bool,
) -> str:
    workspace = f"{public_base_url.rstrip('/')}/calls/{call_id}"
    if kind == "call_started":
        return (
            "A new call connected to the Floodman AI receptionist.\n\n"
            f"Open the secure call workspace to follow its progress:\n{workspace}\n\n"
            "Caller details are available only after signing in."
        )
    return f"{build_message(state, callback_hours, partial=partial)}\n\nSecure call workspace:\n{workspace}"


class TeamNotifier:
    def __init__(self, settings: Settings, database: Database):
        self.settings = settings
        self.database = database
        self.web_push = WebPushService(settings, database)
        self.email = EmailService(settings)
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def call_started(self, call_id: int, state: IntakeState) -> int:
        records = self.web_push.create_call_records(call_id, state, "call_started")
        email_attempts = self._reserve_email_attempts(call_id, state, "call_started")
        if records or email_attempts:
            self._schedule_delivery(
                self._deliver(
                    call_id,
                    state,
                    "call_started",
                    partial=True,
                    push_records=records,
                    sms_attempts=[],
                    email_attempts=email_attempts,
                )
            )
        # Greeting delivery never waits for SMTP or browser push.
        return 0

    def _schedule_delivery(self, delivery: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(delivery)
        self._background_tasks.add(task)
        task.add_done_callback(self._delivery_done)

    def _delivery_done(self, task: asyncio.Task[None]) -> None:
        self._background_tasks.discard(task)
        try:
            task.result()
        except Exception:
            logger.exception("Unable to complete a background team notification")

    async def send(self, call_id: int, state: IntakeState, *, kind: str = "lead", partial: bool = False) -> int:
        sms_attempts: list[tuple[int, str]] = []
        for recipient in recipients(self.settings, state):
            key = f"{state.call_uuid}:{kind}:sms:{recipient}"
            attempt_id = self.database.reserve_notification(call_id, kind, "sms", recipient, key)
            if attempt_id is not None:
                sms_attempts.append((attempt_id, recipient))
        email_attempts = self._reserve_email_attempts(call_id, state, kind)
        records = self.web_push.create_call_records(call_id, state, kind)
        if sms_attempts or email_attempts or records:
            self._schedule_delivery(
                self._deliver(
                    call_id,
                    state,
                    kind,
                    partial=partial,
                    push_records=records,
                    sms_attempts=sms_attempts,
                    email_attempts=email_attempts,
                )
            )
        return len(sms_attempts) + len(email_attempts) + len(records)

    def _reserve_email_attempts(self, call_id: int, state: IntakeState, kind: str) -> list[tuple[int, str]]:
        attempts: list[tuple[int, str]] = []
        for user in self.database.list_email_notification_recipients(kind):
            key = f"{state.call_uuid}:{kind}:email:{user['id']}"
            attempt_id = self.database.reserve_notification(call_id, kind, "email", str(user["email"]), key)
            if attempt_id is not None:
                attempts.append((attempt_id, str(user["email"])))
        return attempts

    async def _deliver(
        self,
        call_id: int,
        state: IntakeState,
        kind: str,
        *,
        partial: bool,
        push_records: list[dict[str, object]],
        sms_attempts: list[tuple[int, str]],
        email_attempts: list[tuple[int, str]],
    ) -> None:
        sms_body = build_message(state, self.settings.callback_sla_hours, partial=partial)
        mail_body = build_email_message(
            state,
            self.settings.callback_sla_hours,
            self.settings.public_base_url,
            call_id,
            kind=kind,
            partial=partial,
        )
        jobs = [self.web_push.send_records(push_records)]
        jobs.extend(self._deliver_sms(attempt_id, recipient, sms_body) for attempt_id, recipient in sms_attempts)
        jobs.extend(
            self._deliver_email(attempt_id, recipient, email_subject(kind), mail_body)
            for attempt_id, recipient in email_attempts
        )
        results = await asyncio.gather(*jobs, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error("Team notification delivery raised %s", type(result).__name__)

    async def _deliver_sms(self, attempt_id: int, recipient: str, body: str) -> None:
        try:
            status, response = await self._send_sms(recipient, body)
        except Exception as exc:
            logger.exception("Unexpected SMS delivery error")
            status, response = "failed", f"Internal SMS delivery error: {type(exc).__name__}"
        self.database.complete_notification(attempt_id, status, response)

    async def _deliver_email(self, attempt_id: int, recipient: str, subject: str, body: str) -> None:
        try:
            status, response = await self.email.send(recipient, subject, body)
        except Exception as exc:
            logger.exception("Unexpected email delivery error")
            status, response = "failed", f"Internal email delivery error: {type(exc).__name__}"
        self.database.complete_notification(attempt_id, status, response)

    async def stop(self, timeout_seconds: float = 10.0) -> None:
        pending = list(self._background_tasks)
        if not pending:
            return
        _, unfinished = await asyncio.wait(pending, timeout=timeout_seconds)
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)

    async def _send_sms(self, recipient: str, body: str) -> tuple[str, str]:
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
