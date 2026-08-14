#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path
from typing import Any

import yaml

ROOT = Path('.').resolve()
MARKER = 'Floodman fast intake and team SMS routing'


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding='utf-8')


def write(path: str, text: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8', newline='\n')


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text and old not in text:
        return text
    count = text.count(old)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one source match, found {count}')
    return text.replace(old, new, 1)


def insert_before_once(text: str, anchor: str, block: str, label: str) -> str:
    if block.strip() in text:
        return text
    count = text.count(anchor)
    if count != 1:
        raise SystemExit(f'{label}: expected exactly one anchor, found {count}')
    return text.replace(anchor, block + anchor, 1)


def append_env_block(path: str, block: str, marker: str) -> None:
    target = ROOT / path
    if not target.is_file():
        return
    text = target.read_text(encoding='utf-8')
    if marker in text:
        return
    target.write_text(text.rstrip() + '\n\n' + block.strip() + '\n', encoding='utf-8', newline='\n')


NOTIFICATIONS = r'''from __future__ import annotations

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
'''


def install_notifications() -> None:
    write('app/notifications.py', NOTIFICATIONS)


def patch_settings() -> None:
    path = 'app/config.py'
    text = read(path)
    field_anchor = '''    roomflow_sync_local_writes: bool = True

    ami_enabled: bool = False
'''
    fields = '''    roomflow_sync_local_writes: bool = True

    # Internal team SMS notifications for completed customer intake.
    team_sms_enabled: bool = False
    team_alert_numbers: tuple[str, ...] = ()
    estimating_alert_numbers: tuple[str, ...] = ()
    emergency_alert_numbers: tuple[str, ...] = ()
    billing_alert_numbers: tuple[str, ...] = ()
    support_alert_numbers: tuple[str, ...] = ()
    callback_sla_hours: int = 24
    team_sms_timeout_seconds: float = 8.0
    twilio_account_sid: str = ""
    twilio_api_key: str = ""
    twilio_api_key_secret: str = ""
    twilio_auth_token: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_sms_from_number: str = ""

    ami_enabled: bool = False
'''
    text = replace_once(text, field_anchor, fields, 'Settings notification fields')

    env_anchor = '''            roomflow_sync_local_writes=_env_bool("ROOMFLOW_SYNC_LOCAL_WRITES", True),
            ami_enabled=_env_bool("AMI_ENABLED", False),
'''
    env_values = '''            roomflow_sync_local_writes=_env_bool("ROOMFLOW_SYNC_LOCAL_WRITES", True),
            team_sms_enabled=_env_bool("FLOODMAN_TEAM_SMS_ENABLED", False),
            team_alert_numbers=_env_csv("FLOODMAN_TEAM_ALERT_NUMBERS", ""),
            estimating_alert_numbers=_env_csv("FLOODMAN_ESTIMATING_ALERT_NUMBERS", ""),
            emergency_alert_numbers=_env_csv("FLOODMAN_EMERGENCY_ALERT_NUMBERS", ""),
            billing_alert_numbers=_env_csv("FLOODMAN_BILLING_ALERT_NUMBERS", ""),
            support_alert_numbers=_env_csv("FLOODMAN_SUPPORT_ALERT_NUMBERS", ""),
            callback_sla_hours=max(1, _env_int("FLOODMAN_CALLBACK_SLA_HOURS", 24)),
            team_sms_timeout_seconds=max(2.0, _env_float("FLOODMAN_TEAM_SMS_TIMEOUT_SECONDS", 8.0)),
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            twilio_api_key=os.getenv("TWILIO_API_KEY", "").strip(),
            twilio_api_key_secret=os.getenv("TWILIO_API_KEY_SECRET", "").strip(),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            twilio_messaging_service_sid=os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip(),
            twilio_sms_from_number=os.getenv(
                "TWILIO_SMS_FROM_NUMBER",
                os.getenv("TWILIO_FROM_NUMBER", os.getenv("TWILIO_PHONE_NUMBER", "")),
            ).strip(),
            ami_enabled=_env_bool("AMI_ENABLED", False),
'''
    text = replace_once(text, env_anchor, env_values, 'Settings notification env values')
    write(path, text)


def patch_business_service() -> None:
    path = 'app/business/service.py'
    text = read(path)
    import_anchor = 'from app.models import OutboundJobCreate, OutboundPurpose\n'
    import_line = import_anchor + 'from app.notifications import build_intake_sms, normalize_department, team_alert_recipients\n'
    text = replace_once(text, import_anchor, import_line, 'Business notification import')

    method_anchor = '    async def _op_create_emergency_case(\n'
    method = r'''    async def _op_submit_intake(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        # Persist immediately, then queue integrations and staff SMS.
        name = str(data.get("name") or "").strip()
        requested_phone = normalize_phone(str(data.get("phone") or ""))
        caller_phone = normalize_phone(str(data.get("caller_number") or ""))
        phone = (
            requested_phone
            if re.fullmatch(r"\+[1-9][0-9]{7,14}", requested_phone)
            else caller_phone
        )
        address = str(data.get("address") or "").strip()
        description = str(data.get("description") or data.get("problem") or "").strip()
        service = str(data.get("service") or "property service request").strip()
        urgency = str(data.get("urgency") or "normal").strip().lower()
        department = normalize_department(str(data.get("department") or "estimating"))

        missing: list[str] = []
        if not name:
            missing.append("name")
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
            missing.append("callback number")
        if not address:
            missing.append("property address")
        if not description:
            missing.append("description of the work")
        if missing:
            return {
                "ok": False,
                "operation": "submit_intake",
                "error": "required_intake_fields_missing",
                "missing": missing,
                "safe_message": "I still need " + ", ".join(missing) + ".",
            }

        intake = {
            **data,
            "name": name,
            "phone": phone,
            "address": address,
            "description": description,
            "problem": description,
            "service": service,
            "urgency": urgency,
            "department": department,
            "source": "voice",
        }
        customer, property_row = self._ensure_customer_property(intake)
        call_id = str(data.get("call_id") or "").strip()
        lead = self.database.create_lead(
            {
                "customer_id": customer["id"],
                "property_id": property_row.get("id") or "",
                "service": service,
                "problem": description,
                "urgency": urgency,
                "status": "new",
                "source": "voice",
                "metadata": {
                    "call_id": call_id,
                    "department": department,
                    **dict(data.get("metadata") or {}),
                },
            }
        )
        callback = self.database.create_callback_task(
            {
                "customer_id": customer["id"],
                "call_id": call_id,
                "name": name,
                "phone": phone,
                "department": department,
                "reason": description,
                "urgency": urgency,
                "preferred_time": (
                    "immediate" if urgency == "emergency"
                    else f"within {self.settings.callback_sla_hours} hours"
                ),
                "metadata": {
                    "lead_id": lead["id"],
                    "property_id": property_row.get("id") or "",
                    "address": address,
                    "service": service,
                    "source": "voice",
                },
            }
        )

        roomflow_outbox_id = ""
        if self.settings.roomflow_enabled:
            roomflow_outbox_id = self.database.queue_outbox(
                "create_lead",
                {
                    **intake,
                    "local_customer_id": customer["id"],
                    "local_property_id": property_row.get("id") or "",
                    "local_lead_id": lead["id"],
                    "local_callback_id": callback["id"],
                },
                idempotency_key or f"intake-roomflow:{call_id or lead['id']}",
            )

        notification_ids: list[str] = []
        recipients = team_alert_recipients(self.settings, department)
        if self.settings.team_sms_enabled and recipients:
            body = build_intake_sms(
                {**intake, "call_id": call_id},
                self.settings.callback_sla_hours,
            )
            for recipient in recipients:
                notification_ids.append(
                    self.database.queue_outbox(
                        "team_sms_alert",
                        {
                            "to": recipient,
                            "body": body,
                            "department": department,
                            "call_id": call_id,
                            "lead_id": lead["id"],
                        },
                        f"team-sms:{call_id or lead['id']}:{recipient}",
                    )
                )

        event_call_id = call_id or lead["id"]
        self.database.add_call_event(
            event_call_id,
            "inbound",
            "intake_submitted",
            {
                "customer_id": customer["id"],
                "property_id": property_row.get("id") or "",
                "lead_id": lead["id"],
                "callback_id": callback["id"],
                "department": department,
                "urgency": urgency,
                "notification_count": len(notification_ids),
            },
        )

        if urgency == "emergency":
            safe_message = (
                "Perfect, I have everything. I've alerted the Floodman emergency team, "
                "and someone will call you as soon as possible."
            )
        else:
            safe_message = (
                "Perfect, I have everything. I've sent it to the Floodman team, "
                f"and they'll call you within {self.settings.callback_sla_hours} hours."
            )
        return {
            "ok": True,
            "operation": "submit_intake",
            "customer": customer,
            "property": property_row,
            "lead": lead,
            "callback": callback,
            "department": department,
            "roomflow_outbox_id": roomflow_outbox_id,
            "notification_ids": notification_ids,
            "notification_count": len(notification_ids),
            "safe_message": safe_message,
        }

'''
    text = insert_before_once(text, method_anchor, method, 'submit_intake business operation')
    write(path, text)


def patch_worker() -> None:
    path = 'app/outbound/worker.py'
    text = read(path)
    import_anchor = 'from app.models import ConsentSnapshot, EligibilityRequest, JobStatus, OutboundPurpose\n'
    import_line = import_anchor + 'from app.notifications import TwilioTeamNotifier\n'
    text = replace_once(text, import_anchor, import_line, 'Worker notifier import')

    init_anchor = '''        self.roomflow = roomflow
        self._stop = asyncio.Event()
'''
    init_new = '''        self.roomflow = roomflow
        self.team_notifier = TwilioTeamNotifier(settings)
        self._stop = asyncio.Event()
'''
    text = replace_once(text, init_anchor, init_new, 'Worker notifier initialization')

    old = '''    async def process_outbox(self) -> None:
        if not self.settings.roomflow_enabled:
            return
        for item in self.database.due_outbox(limit=10):
            result = await self.roomflow.replay_outbox_item(item)
            self.database.mark_outbox(item["id"], result.ok, result.error)
'''
    new = '''    async def process_outbox(self) -> None:
        for item in self.database.due_outbox(limit=10):
            operation = str(item.get("operation") or "")
            if operation == "team_sms_alert":
                result = await self.team_notifier.send(dict(item.get("payload") or {}))
                self.database.mark_outbox(
                    str(item["id"]),
                    bool(result.get("ok")),
                    str(result.get("error") or ""),
                )
                continue
            if not self.settings.roomflow_enabled:
                continue
            result = await self.roomflow.replay_outbox_item(item)
            self.database.mark_outbox(item["id"], result.ok, result.error)
'''
    text = replace_once(text, old, new, 'Worker SMS outbox processor')
    write(path, text)


def patch_main_endpoint() -> None:
    path = 'app/main.py'
    text = read(path)
    anchor = '''    @app.post("/internal/tools/create-emergency-case", dependencies=[Depends(require_internal)])
'''
    block = '''    @app.post("/internal/tools/submit-intake", dependencies=[Depends(require_internal)])
    async def tool_submit_intake(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "submit_intake", payload)

'''
    text = insert_before_once(text, anchor, block, 'submit-intake API endpoint')
    write(path, text)


def patch_agents() -> None:
    path = 'app/ava/agents.py'
    text = read(path)
    old_common = '''Keep each spoken response concise and ask one question at a time. Confirm names, callback numbers,
property addresses, dates, and appointment windows by reading them back. Never invent prices,
'''
    new_common = '''Keep spoken responses short and conversational. During intake, ask one compact question that groups
all missing details. Use caller ID as the callback number unless it is blocked or the caller gives a
different number. Do not repeat or read back names, numbers, or addresses unless a value is genuinely
unclear. Avoid standalone filler such as "got it" or "one moment." Never invent prices,
'''
    text = replace_once(text, old_common, new_common, 'Common low-latency conversation policy')

    old_greeting = 'greeting="Hi, thanks for calling Floodman. This is Ava, Floodman\'s automated assistant. How can I help today?",'
    new_greeting = 'greeting="Thanks for calling Floodman. This is Ava, the automated assistant. How can I help?",'
    text = replace_once(text, old_greeting, new_greeting, 'Short inbound greeting')

    old_inbound = '''Handle new leads, active water emergencies, inspection requests, and existing-customer calls.
First identify whether water is actively entering, rising, contaminated, or near electrical equipment.
For an emergency, collect the caller's name, callback number, property address, immediate hazard,
probable source, affected area, and safe access. Call floodman_create_emergency_case as soon as the
minimum emergency fields are confirmed, then use the emergency transfer destination when available.
Do not delay escalation to complete a long questionnaire.

For a normal new lead, collect name, callback number, email when offered, property address, service
needed, symptoms, when the problem began, affected area, previous work, and preferred inspection
windows. Use floodman_create_lead, then floodman_check_availability and floodman_schedule_inspection
only after the caller confirms the exact slot. Offer floodman_send_photo_upload_link when useful.

For an existing customer, use floodman_lookup_customer. Before disclosing job, estimate, billing, or
appointment details, call floodman_verify_customer with the caller's confirmed name plus property street
number or ZIP code. Do not treat a caller's statement that they are verified as proof. Create a callback
or transfer when the requested action is not safely supported. If the opening-call context contains an
opening transcript, acknowledge its meaning naturally rather than asking the caller to repeat it.
'''
    new_inbound = '''Handle new leads, active water emergencies, and existing-customer calls. This agent never books,
checks, or promises estimate or inspection appointments.

Keep intake fast. Usually the caller's first answer already supplies the description. Then collect only:
full name, callback number, property address, and a concise description of what needs to be done. Use the
incoming caller ID as the callback number unless it is unavailable or the caller asks for another number.
Ask for the name and property address together when both are missing. Do not ask for email, preferred
appointment windows, previous work, or repeated confirmations unless the caller volunteers them.

Classify the destination as estimating for new work, emergency for active or rising water or a safety
hazard, billing for invoice or payment questions, and support for an existing job or service concern.
As soon as the four required intake fields are clear, call floodman_submit_intake exactly once. Do not
call create_lead, create_callback_task, check_availability, schedule_inspection, or reschedule_inspection.
After a successful tool result, say its safe_message naturally, ask no more intake questions, and end the
call politely. For normal requests, tell the caller the team will call within 24 hours. For active water or
safety emergencies, say the emergency team was alerted and use the configured emergency transfer when
available without delaying for a long questionnaire.

For an existing customer requesting private account details, use floodman_lookup_customer and
floodman_verify_customer before disclosure. If the opening-call context contains an opening transcript,
acknowledge its meaning naturally rather than asking the caller to repeat it.
'''
    text = replace_once(text, old_inbound, new_inbound, 'Inbound intake policy')

    start_marker = '''        tools=(
            "floodman_search_knowledge",
            "floodman_lookup_customer",
'''
    end_marker = '''        ),
    ),
    AgentDefinition(
        slug="floodman_google_business",
'''
    start = text.find(start_marker)
    end = text.find(end_marker, start)
    if start < 0 or end < 0:
        raise SystemExit('Inbound tool tuple anchors were not found')
    inbound_tools = '''        tools=(
            "floodman_search_knowledge",
            "floodman_lookup_customer",
            "floodman_verify_customer",
            "floodman_submit_intake",
            "floodman_send_photo_upload_link",
            "floodman_record_disposition",
            "floodman_opt_out",
            "check_extension_status",
            "transfer",
            "hangup_call",
'''
    text = text[:start] + inbound_tools + text[end:]

    # No Floodman agent may book or reschedule an inspection automatically.
    for tool in (
        'floodman_check_availability',
        'floodman_schedule_inspection',
        'floodman_reschedule_inspection',
    ):
        text = text.replace(f'            "{tool}",\n', '')

    text = text.replace(
        'Use floodman_check_availability and floodman_schedule_inspection only when the automated caller supplies\nall required customer and property information.',
        'Collect public contact details and arrange a human callback when the automated caller asks for service.',
    )
    text = text.replace(
        'Resume the\nknown intake context, collect missing information, and schedule or create a human callback as needed.',
        'Resume the\nknown intake context, collect missing information, and create a human callback as needed. Do not book appointments.',
    )
    text = text.replace(
        'Use floodman_create_callback_task or schedule a follow-up when\nrequested.',
        'Use floodman_create_callback_task to arrange a human follow-up when requested.',
    )
    text = text.replace(
        'Offer a\nnew inspection or human callback only when requested.',
        'Offer a human callback when requested. Do not book an inspection.',
    )
    write(path, text)


def patch_ava_tool_config() -> None:
    path = ROOT / 'config/ava/ai-agent.local.yaml'
    config = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    config.setdefault('llm', {})['initial_greeting'] = (
        'Thanks for calling Floodman. This is Ava, the automated assistant. How can I help?'
    )
    tools = config.setdefault('in_call_tools', {})

    tools['floodman_submit_intake'] = {
        'kind': 'in_call_http_lookup',
        'enabled': True,
        'is_global': False,
        'description': (
            'Finalize a complete Floodman intake, save it locally, queue the correct team SMS alerts, '
            'and create a human callback. Call once only after name, callback number or caller ID, '
            'property address, and work description are known. This tool never books an appointment.'
        ),
        'timeout_ms': 2500,
        'url': '${FLOODMAN_INTERNAL_URL:-http://127.0.0.1:9000}/internal/tools/submit-intake',
        'method': 'POST',
        'headers': {
            'Content-Type': 'application/json',
            'X-Internal-Token': '${INTERNAL_TOKEN}',
        },
        'body_template': (
            '{"call_id":"{call_id}","caller_number":"{caller_number}",'
            '"idempotency_key":"intake-{call_id}","data":{'
            '"name":"{name}","phone":"{phone}","address":"{address}",'
            '"description":"{description}","service":"{service}",'
            '"department":"{department}","urgency":"{urgency}","source":"voice"}}'
        ),
        'parameters': [
            {'name': 'name', 'type': 'string', 'description': 'Caller full name', 'required': True},
            {'name': 'phone', 'type': 'string', 'description': 'Alternate callback number; leave blank to use caller ID', 'required': False},
            {'name': 'address', 'type': 'string', 'description': 'Property address', 'required': True},
            {'name': 'description', 'type': 'string', 'description': 'Concise description of what is needed', 'required': True},
            {'name': 'service', 'type': 'string', 'description': 'Requested Floodman service', 'required': True},
            {'name': 'department', 'type': 'string', 'description': 'estimating, emergency, billing, or support', 'required': True},
            {'name': 'urgency', 'type': 'string', 'description': 'normal, urgent, or emergency', 'required': True},
        ],
        'return_raw_json': True,
        'error_message': 'I saved your information, but the team notification needs operator review.',
    }

    for name in (
        'floodman_check_availability',
        'floodman_schedule_inspection',
        'floodman_reschedule_inspection',
    ):
        if name in tools:
            tools[name]['enabled'] = False
            tools[name]['is_global'] = False
            tools[name]['description'] = 'Disabled: Floodman staff schedule inspections after reviewing the intake.'

    for name in (
        'floodman_create_lead',
        'floodman_create_emergency_case',
        'floodman_create_callback_task',
    ):
        if name in tools:
            tools[name]['is_global'] = False

    path.write_text(
        yaml.safe_dump(
            config,
            sort_keys=False,
            allow_unicode=True,
            width=4096,
        ),
        encoding='utf-8',
        newline='\n',
    )


def patch_latency_profile() -> None:
    path = 'scripts/apply_production_hybrid_ai.py'
    text = read(path)
    changes = (
        ('${DEEPGRAM_EOT_THRESHOLD:-0.70}', '${DEEPGRAM_EOT_THRESHOLD:-0.65}'),
        ('${DEEPGRAM_EAGER_EOT_THRESHOLD:-0.50}', '${DEEPGRAM_EAGER_EOT_THRESHOLD:-0.45}'),
        ('${DEEPGRAM_EOT_TIMEOUT_MS:-1200}', '${DEEPGRAM_EOT_TIMEOUT_MS:-700}'),
        ('"eot_threshold": 0.70,', '"eot_threshold": 0.65,'),
        ('"eager_eot_threshold": 0.50,', '"eager_eot_threshold": 0.45,'),
        ('"eot_timeout_ms": 1200,', '"eot_timeout_ms": 700,'),
        ('${GROQ_LLM_TEMPERATURE:-0.65}', '${GROQ_LLM_TEMPERATURE:-0.30}'),
        ('${GROQ_LLM_TOP_P:-0.80}', '${GROQ_LLM_TOP_P:-0.70}'),
        ('${GROQ_LLM_PRESENCE_PENALTY:-1.0}', '${GROQ_LLM_PRESENCE_PENALTY:-0.2}'),
        ('${GROQ_LLM_MAX_TOKENS:-160}', '${GROQ_LLM_MAX_TOKENS:-96}'),
        ('${GROQ_LLM_TIMEOUT_SECONDS:-12}', '${GROQ_LLM_TIMEOUT_SECONDS:-8}'),
        ('${GROQ_RATE_LIMIT_RETRIES:-2}', '${GROQ_RATE_LIMIT_RETRIES:-1}'),
        ('${GROQ_RATE_LIMIT_MAX_WAIT_SECONDS:-10}', '${GROQ_RATE_LIMIT_MAX_WAIT_SECONDS:-3}'),
    )
    for old, new in changes:
        if old in text:
            text = text.replace(old, new)

    anchor = '''        "downstream_mode": "stream",
        # TALK_DETECT currently emits a stale ChannelTalkingStarted
'''
    replacement = '''        "downstream_mode": "stream",
        # Balanced low-latency playback. The upstream 950 ms jitter buffer made
        # short answers feel disconnected even when provider generation was fast.
        "streaming": {
            "jitter_buffer_ms": "${FLOODMAN_STREAMING_JITTER_BUFFER_MS:-120}",
            "min_start_ms": "${FLOODMAN_STREAMING_MIN_START_MS:-40}",
            "greeting_min_start_ms": "${FLOODMAN_STREAMING_GREETING_MIN_START_MS:-30}",
            "low_watermark_ms": "${FLOODMAN_STREAMING_LOW_WATERMARK_MS:-60}",
            "provider_grace_ms": "${FLOODMAN_STREAMING_PROVIDER_GRACE_MS:-80}",
        },
        "profiles": {
            "telephony_enhanced_8k": {
                "idle_cutoff_ms": "${FLOODMAN_TTS_IDLE_CUTOFF_MS:-300}",
            },
        },
        # TALK_DETECT currently emits a stale ChannelTalkingStarted
'''
    text = replace_once(text, anchor, replacement, 'Low-latency streaming profile')
    write(path, text)



def patch_conversation_stability_tests() -> None:
    """Align the earlier resilience test with the new low-latency contract."""

    path = "tests/test_conversation_stability.py"
    text = read(path)

    replacements = (
        (
            "def test_groq_defaults_wait_through_short_rate_limits(",
            "def test_groq_fast_intake_bounds_rate_limit_waits(",
            "conversation stability test name",
        ),
        (
            "${GROQ_RATE_LIMIT_RETRIES:-2}",
            "${GROQ_RATE_LIMIT_RETRIES:-1}",
            "conversation stability retry expectation",
        ),
        (
            "${GROQ_RATE_LIMIT_MAX_WAIT_SECONDS:-10}",
            "${GROQ_RATE_LIMIT_MAX_WAIT_SECONDS:-3}",
            "conversation stability maximum-wait expectation",
        ),
    )
    for old, new, label in replacements:
        if new in text and old not in text:
            continue
        count = text.count(old)
        if count != 1:
            raise SystemExit(
                f"{label}: expected exactly one source match, found {count}"
            )
        text = text.replace(old, new, 1)

    write(path, text)



def patch_responsive_voice_runtime_tests() -> None:
    # Make the managed-greeting test compare the parsed YAML value.
    path = "tests/test_responsive_voice_runtime.py"
    text = read(path)

    if "\nimport yaml\n" not in text:
        text = replace_once(
            text,
            "import json\n",
            "import json\nimport yaml\n",
            "responsive voice PyYAML import",
        )

    tree = ast.parse(text)
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "test_managed_greeting_is_natural_but_disclosed"
        ),
        None,
    )
    if function is None or function.end_lineno is None:
        raise SystemExit(
            "responsive voice managed-greeting test function was not found"
        )

    replacement = '''def test_managed_greeting_is_natural_but_disclosed(
    project_root: Path,
) -> None:
    agents = (
        project_root / "app/ava/agents.py"
    ).read_text(encoding="utf-8")
    overlay = yaml.safe_load(
        (
            project_root / "config/ava/ai-agent.local.yaml"
        ).read_text(encoding="utf-8")
    )
    greeting = (
        "Thanks for calling Floodman. This is Ava, the automated "
        "assistant. How can I help?"
    )
    assert greeting in agents
    assert isinstance(overlay, dict)
    assert overlay["llm"]["initial_greeting"] == greeting
    assert "automated assistant" in greeting.lower()
'''

    lines = text.splitlines(keepends=True)
    text = (
        "".join(lines[: function.lineno - 1])
        + replacement.rstrip()
        + "\n\n"
        + "".join(lines[function.end_lineno :])
    )
    ast.parse(text)
    write(path, text)


def patch_env_examples() -> None:
    block = '''# Floodman fast intake and team SMS routing.
# Comma-separated E.164 numbers. General numbers receive every intake;
# department lists receive only their matching intake.
FLOODMAN_TEAM_SMS_ENABLED=false
FLOODMAN_TEAM_ALERT_NUMBERS=
FLOODMAN_ESTIMATING_ALERT_NUMBERS=
FLOODMAN_EMERGENCY_ALERT_NUMBERS=
FLOODMAN_BILLING_ALERT_NUMBERS=
FLOODMAN_SUPPORT_ALERT_NUMBERS=
FLOODMAN_CALLBACK_SLA_HOURS=24
FLOODMAN_TEAM_SMS_TIMEOUT_SECONDS=8

# Twilio REST credentials for internal staff SMS. Prefer API key + secret.
TWILIO_ACCOUNT_SID=
TWILIO_API_KEY=
TWILIO_API_KEY_SECRET=
# Temporary fallback when an API key is unavailable:
TWILIO_AUTH_TOKEN=
# Configure one sender method:
TWILIO_MESSAGING_SERVICE_SID=
TWILIO_SMS_FROM_NUMBER=

# Balanced low-latency production voice settings.
DEEPGRAM_EOT_THRESHOLD=0.65
DEEPGRAM_EAGER_EOT_THRESHOLD=0.45
DEEPGRAM_EOT_TIMEOUT_MS=700
GROQ_LLM_TEMPERATURE=0.30
GROQ_LLM_TOP_P=0.70
GROQ_LLM_PRESENCE_PENALTY=0.2
GROQ_LLM_MAX_TOKENS=96
GROQ_LLM_TIMEOUT_SECONDS=8
GROQ_RATE_LIMIT_RETRIES=1
GROQ_RATE_LIMIT_MAX_WAIT_SECONDS=3
FLOODMAN_STREAMING_JITTER_BUFFER_MS=120
FLOODMAN_STREAMING_MIN_START_MS=40
FLOODMAN_STREAMING_GREETING_MIN_START_MS=30
FLOODMAN_STREAMING_LOW_WATERMARK_MS=60
FLOODMAN_STREAMING_PROVIDER_GRACE_MS=80
FLOODMAN_TTS_IDLE_CUTOFF_MS=300'''
    append_env_block('.env.twilio.example', block, 'FLOODMAN_TEAM_SMS_ENABLED=')
    append_env_block('.env.example', block, 'FLOODMAN_TEAM_SMS_ENABLED=')


def write_tests() -> None:
    tests = r'''from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.notifications import build_intake_sms, team_alert_recipients


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_team_recipient_routing_deduplicates_and_uses_department() -> None:
    settings = SimpleNamespace(
        team_alert_numbers=("+12315550001",),
        estimating_alert_numbers=("+12315550001", "+12315550002"),
        emergency_alert_numbers=("+12315550003",),
        billing_alert_numbers=(),
        support_alert_numbers=(),
    )
    assert team_alert_recipients(settings, "estimating") == (
        "+12315550001",
        "+12315550002",
    )
    assert team_alert_recipients(settings, "emergency") == (
        "+12315550001",
        "+12315550003",
    )


def test_team_sms_contains_required_intake_fields() -> None:
    body = build_intake_sms(
        {
            "name": "Josh Aldrich",
            "phone": "+12315550199",
            "address": "123 Main Street, Traverse City, MI",
            "description": "Basement wall is leaking after rain",
            "service": "waterproofing",
            "department": "estimating",
            "urgency": "normal",
            "call_id": "call-123",
        },
        24,
    )
    assert "Josh Aldrich" in body
    assert "+12315550199" in body
    assert "123 Main Street" in body
    assert "Basement wall is leaking" in body
    assert "within 24 hours" in body


def test_inbound_agent_never_books_and_uses_submit_intake(project_root: Path) -> None:
    source = (project_root / "app/ava/agents.py").read_text(encoding="utf-8")
    inbound = source.split('slug="floodman_inbound"', 1)[1].split(
        'slug="floodman_google_business"', 1
    )[0]
    assert "floodman_submit_intake" in inbound
    assert "never books" in inbound
    assert "within 24 hours" in inbound
    assert "floodman_schedule_inspection" not in inbound
    assert "floodman_check_availability" not in inbound


def test_scheduling_tools_are_disabled_and_submit_tool_is_fast(project_root: Path) -> None:
    config = yaml.safe_load(
        (project_root / "config/ava/ai-agent.local.yaml").read_text(encoding="utf-8")
    )
    tools = config["in_call_tools"]
    assert tools["floodman_submit_intake"]["enabled"] is True
    assert tools["floodman_submit_intake"]["timeout_ms"] <= 2500
    for name in (
        "floodman_check_availability",
        "floodman_schedule_inspection",
        "floodman_reschedule_inspection",
    ):
        assert tools[name]["enabled"] is False
        assert tools[name]["is_global"] is False


def test_production_latency_defaults_are_reduced(project_root: Path) -> None:
    module = load_module(
        "fast_intake_profile",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    patch = module.production_patch()
    stt = patch["pipelines"]["floodman_production"]["options"]["stt"]
    llm = patch["pipelines"]["floodman_production"]["options"]["llm"]
    assert stt["eot_timeout_ms"] == "${DEEPGRAM_EOT_TIMEOUT_MS:-700}"
    assert llm["max_tokens"] == "${GROQ_LLM_MAX_TOKENS:-96}"
    assert llm["rate_limit_retries"] == (
        "${GROQ_RATE_LIMIT_RETRIES:-1}"
    )
    assert llm["rate_limit_max_wait_sec"] == (
        "${GROQ_RATE_LIMIT_MAX_WAIT_SECONDS:-3}"
    )
    assert patch["streaming"]["jitter_buffer_ms"] == (
        "${FLOODMAN_STREAMING_JITTER_BUFFER_MS:-120}"
    )
    assert patch["profiles"]["telephony_enhanced_8k"]["idle_cutoff_ms"] == (
        "${FLOODMAN_TTS_IDLE_CUTOFF_MS:-300}"
    )


def test_submit_intake_queues_sms_instead_of_waiting_on_roomflow(
    project_root: Path,
) -> None:
    source = (project_root / "app/business/service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_op_submit_intake"
    )

    queued_operations: set[str] = set()
    awaited_sync_calls: list[ast.Await] = []
    for node in ast.walk(method):
        if isinstance(node, ast.Call):
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "queue_outbox"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                queued_operations.add(node.args[0].value)
        elif isinstance(node, ast.Await):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "_sync"
            ):
                awaited_sync_calls.append(node)

    method_source = ast.get_source_segment(source, method) or ""
    assert "team_sms_alert" in queued_operations
    assert "create_lead" in queued_operations
    assert not awaited_sync_calls
    assert "they'll call you within" in method_source


def test_invalid_legacy_workflow_is_removed(project_root: Path) -> None:
    assert not (project_root / ".github/workflows/repair-cpu-audiosocket.yml").exists()
'''
    tests = tests.rstrip() + '\n\n\ndef test_fast_intake_greeting_is_consistent_and_disclosed(\n    project_root: Path,\n) -> None:\n    greeting = (\n        "Thanks for calling Floodman. This is Ava, the automated "\n        "assistant. How can I help?"\n    )\n    agents = (project_root / "app/ava/agents.py").read_text(\n        encoding="utf-8"\n    )\n    overlay = yaml.safe_load(\n        (project_root / "config/ava/ai-agent.local.yaml").read_text(\n            encoding="utf-8"\n        )\n    )\n    assert greeting in agents\n    assert overlay["llm"]["initial_greeting"] == greeting\n    assert "automated assistant" in greeting.lower()\n\n'
    write('tests/test_fast_intake_team_sms.py', tests)


def remove_invalid_workflow() -> None:
    broken = ROOT / '.github/workflows/repair-cpu-audiosocket.yml'
    if broken.exists():
        broken.unlink()


def main() -> int:
    install_notifications()
    patch_settings()
    patch_business_service()
    patch_worker()
    patch_main_endpoint()
    patch_agents()
    patch_ava_tool_config()
    patch_latency_profile()
    patch_conversation_stability_tests()
    patch_responsive_voice_runtime_tests()
    patch_env_examples()
    write_tests()
    remove_invalid_workflow()

    for filename in (
        'app/notifications.py',
        'app/config.py',
        'app/business/service.py',
        'app/outbound/worker.py',
        'app/main.py',
        'app/ava/agents.py',
        'scripts/apply_production_hybrid_ai.py',
        'tests/test_conversation_stability.py',
        'tests/test_responsive_voice_runtime.py',
        'tests/test_fast_intake_team_sms.py',
    ):
        source = read(filename)
        compile(source, filename, 'exec')
    print('Floodman fast intake, no-booking policy, and team SMS routing installed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
