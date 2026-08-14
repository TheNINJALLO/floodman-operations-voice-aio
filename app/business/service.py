from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from app.calendar import LocalAvailability
from app.compliance.engine import normalize_phone
from app.config import Settings
from app.db import Database
from app.models import OutboundJobCreate, OutboundPurpose
from app.notifications import build_intake_sms, normalize_department, team_alert_recipients
from app.roomflow.client import RoomflowClient, RoomflowResult
from app.security import SignedTokenManager


class BusinessOperations:
    """Business-safe facade used by AVA tools.

    Local writes happen first so a carrier, model, Roomflow, or calendar outage cannot
    discard caller information. Roomflow synchronization is then attempted through the
    durable integration outbox.
    """

    def __init__(self, settings: Settings, database: Database, roomflow: RoomflowClient):
        self.settings = settings
        self.database = database
        self.roomflow = roomflow
        self.availability = LocalAvailability(settings, database)
        self.tokens = SignedTokenManager(settings.upload_token_secret)

    async def execute(
        self,
        operation: str,
        payload: dict[str, Any],
        *,
        call_id: str = "",
        caller_number: str = "",
        customer_id: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        data = dict(payload)
        if call_id:
            data["call_id"] = call_id
        else:
            data.setdefault("call_id", "")
        if caller_number:
            data["caller_number"] = normalize_phone(caller_number)
        else:
            data.setdefault("caller_number", "")
        if customer_id:
            # The verified envelope is authoritative. Never let model-supplied
            # body data switch the customer after the API verification check.
            data["customer_id"] = customer_id
        else:
            data.setdefault("customer_id", "")
        handler = getattr(self, f"_op_{operation}", None)
        if handler is None:
            result = await self.roomflow.execute(
                operation, data, idempotency_key=idempotency_key
            )
            return result.model_dump()
        return await handler(data, idempotency_key=idempotency_key)

    async def _sync(
        self, operation: str, payload: dict[str, Any], idempotency_key: str
    ) -> RoomflowResult:
        return await self.roomflow.execute(
            operation,
            payload,
            idempotency_key=idempotency_key,
            queue_on_failure=self.settings.roomflow_sync_local_writes,
        )

    async def _op_lookup_customer(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        phone = normalize_phone(str(data.get("phone") or data.get("caller_number") or ""))
        name = str(data.get("name") or "").strip()
        address = str(data.get("address") or "").strip()

        remote = await self.roomflow.execute(
            "lookup_customer",
            {"phone": phone, "name": name, "address": address},
            queue_on_failure=False,
        )
        local = self.database.search_customers(phone=phone, name=name, address=address, limit=20)
        if remote.ok:
            self._ingest_remote_customer(remote.data)
            refreshed = self.database.search_customers(
                phone=phone, name=name, address=address, limit=20
            )
            if refreshed:
                local = refreshed
        candidates = [self._safe_customer_candidate(value) for value in local]
        return {
            "ok": bool(candidates) or remote.ok,
            "operation": "lookup_customer",
            "customers": candidates,
            "remote": self._safe_roomflow_status(remote),
            "found": bool(candidates) or bool(remote.data),
            "match_count": len(candidates),
            "safe_message": (
                "I found a possible customer record. Please verify the caller before discussing account details."
                if candidates
                else "I did not find a matching local customer record."
            ),
        }

    async def _op_create_lead(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        customer, property_row = self._ensure_customer_property(data)
        lead = self.database.create_lead(
            {
                "customer_id": customer["id"],
                "property_id": property_row.get("id") or "",
                "service": data.get("service") or "inspection",
                "problem": data.get("problem") or "",
                "urgency": data.get("urgency") or "normal",
                "status": data.get("status") or "new",
                "source": data.get("source") or "voice",
                "metadata": {"call_id": data.get("call_id"), **dict(data.get("metadata") or {})},
            }
        )
        sync_payload = {
            **data,
            "local_customer_id": customer["id"],
            "local_property_id": property_row.get("id") or "",
            "local_lead_id": lead["id"],
        }
        sync = await self._sync(
            "create_lead", sync_payload, idempotency_key or f"lead:{lead['id']}"
        )
        return {
            "ok": True,
            "operation": "create_lead",
            "customer": customer,
            "property": property_row,
            "lead": lead,
            "roomflow": sync.model_dump(),
            "queued": sync.queued,
        }

    async def _op_submit_intake(
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

    async def _op_create_emergency_case(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        emergency = dict(data)
        emergency["urgency"] = "emergency"
        emergency["service"] = emergency.get("service") or "emergency_water_response"
        emergency["problem"] = emergency.get("problem") or self._emergency_summary(emergency)
        lead_result = await self._op_create_lead(
            emergency, idempotency_key=idempotency_key or f"emergency:{data.get('call_id') or uuid.uuid4()}"
        )
        callback = self.database.create_callback_task(
            {
                "customer_id": lead_result["customer"]["id"],
                "call_id": data.get("call_id") or "",
                "name": data.get("name") or "",
                "phone": normalize_phone(str(data.get("phone") or data.get("caller_number") or "")),
                "department": "emergency",
                "reason": emergency["problem"],
                "urgency": "emergency",
                "preferred_time": "immediate",
                "metadata": {"lead_id": lead_result["lead"]["id"]},
            }
        )
        self.database.add_call_event(
            str(data.get("call_id") or lead_result["lead"]["id"]),
            "inbound",
            "emergency_case_created",
            {"lead_id": lead_result["lead"]["id"], "callback_id": callback["id"]},
        )
        return {**lead_result, "operation": "create_emergency_case", "callback": callback}

    async def _op_check_availability(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        remote = await self.roomflow.execute(
            "check_availability", data, queue_on_failure=False
        )
        if remote.ok and remote.data:
            return {
                "ok": True,
                "operation": "check_availability",
                "source": "roomflow",
                "slots": remote.data.get("slots", remote.data),
                "roomflow": remote.model_dump(),
            }
        slots = self.availability.slots(data)
        return {
            "ok": True,
            "operation": "check_availability",
            "source": "local_fallback",
            "slots": slots,
            "roomflow": self._safe_roomflow_status(remote),
            "safe_message": (
                "These are provisional Floodman openings. The selected appointment is confirmed only after booking succeeds."
            ),
        }

    async def _op_schedule_inspection(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        existing_customer = self.database.get_customer(str(data.get("customer_id") or ""))
        if not existing_customer:
            name = str(data.get("name") or "").strip()
            phone = normalize_phone(str(data.get("phone") or data.get("caller_number") or ""))
            address = str(data.get("address") or "").strip()
            if not name or not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone) or not address:
                return {
                    "ok": False,
                    "operation": "schedule_inspection",
                    "error": "name_phone_and_property_address_required",
                    "safe_message": "I need the customer's name, a valid callback number, and the property address before scheduling.",
                }
        timezone_name = str(data.get("timezone") or self.settings.timezone)
        validation = self.availability.validate_slot(
            str(data.get("start") or ""), str(data.get("end") or ""), timezone_name
        )
        if not validation.get("ok"):
            return {
                "ok": False,
                "operation": "schedule_inspection",
                **validation,
                "safe_message": "That appointment time is not available under Floodman's scheduling rules. Please check availability again.",
            }
        customer, property_row = self._ensure_customer_property(data)
        start = str(validation["start"])
        end = str(validation["end"])
        appointment = self.database.create_appointment(
            {
                "customer_id": customer["id"],
                "property_id": property_row.get("id") or "",
                "service": data.get("service") or "inspection",
                "start": start,
                "end": end,
                "timezone": validation["timezone"],
                "status": "scheduled",
                "metadata": {"slot_id": data.get("slot_id"), "call_id": data.get("call_id")},
            }
        )
        sync_payload = {
            **data,
            "start": start,
            "end": end,
            "local_customer_id": customer["id"],
            "local_property_id": property_row.get("id") or "",
            "local_appointment_id": appointment["id"],
        }
        sync = await self._sync(
            "schedule_inspection",
            sync_payload,
            idempotency_key or f"appointment:{appointment['id']}",
        )
        return {
            "ok": True,
            "operation": "schedule_inspection",
            "appointment": appointment,
            "customer": self._safe_customer_candidate(self.database.customer_bundle(customer["id"])),
            "property": {"id": property_row.get("id"), "address_confirmed": bool(property_row)},
            "roomflow": self._safe_roomflow_status(sync),
            "queued": sync.queued,
            "confirmed_locally": True,
            "confirmed_in_roomflow": sync.ok,
        }

    async def _op_reschedule_inspection(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        appointment_id = str(data.get("appointment_id") or "")
        timezone_name = str(data.get("timezone") or self.settings.timezone)
        validation = self.availability.validate_slot(
            str(data.get("start") or ""),
            str(data.get("end") or ""),
            timezone_name,
            exclude_id=appointment_id,
        )
        if not validation.get("ok"):
            return {
                "ok": False,
                "operation": "reschedule_inspection",
                **validation,
                "safe_message": "That replacement appointment time is unavailable. Please check availability again.",
            }
        updated = self.database.reschedule_appointment(
            appointment_id,
            {
                "start": validation["start"],
                "end": validation["end"],
                "timezone": validation["timezone"],
                "status": "scheduled",
            },
        )
        if not updated:
            return {
                "ok": False,
                "operation": "reschedule_inspection",
                "error": "appointment_not_found",
            }
        sync = await self._sync(
            "reschedule_inspection",
            {**data, "start": validation["start"], "end": validation["end"], "local_appointment_id": appointment_id},
            idempotency_key or f"reschedule:{appointment_id}:{updated['start_at']}",
        )
        return {
            "ok": True,
            "operation": "reschedule_inspection",
            "appointment": updated,
            "roomflow": self._safe_roomflow_status(sync),
            "queued": sync.queued,
        }

    async def _op_send_photo_upload_link(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        token = self.tokens.create(
            {
                "customer_id": str(data.get("customer_id") or ""),
                "call_id": str(data.get("call_id") or ""),
                "purpose": "property_photos",
            },
            ttl_seconds=int(self.settings.upload_config.get("link_ttl_hours", 168)) * 3600,
        )
        url = f"{self.settings.public_base_url}/upload/{token}"
        sync_payload = {**data, "upload_url": url}
        sync = await self._sync(
            "send_photo_upload_link",
            sync_payload,
            idempotency_key or f"photo-link:{data.get('call_id') or token[:16]}",
        )
        return {
            "ok": True,
            "operation": "send_photo_upload_link",
            "upload_url": url,
            "delivery": sync.model_dump(),
            "delivered": sync.ok,
            "queued": sync.queued,
        }

    async def _op_verify_customer_identity(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        call_id = str(data.get("call_id") or "")
        customer_id = str(data.get("customer_id") or "")
        bundle = self.database.customer_bundle(customer_id) if customer_id else {}
        matches = self._identity_matches(bundle, data) if bundle else {}
        required = int(self.settings.compliance_config.get("identity_required_matches", 2))
        verified = sum(1 for value in matches.values() if value) >= required

        remote = await self.roomflow.execute(
            "verify_customer_identity", data, queue_on_failure=False
        )
        if remote.ok and isinstance(remote.data, dict):
            remote_verified = bool(remote.data.get("verified"))
            if remote_verified:
                verified = True
                matches["roomflow"] = True

        if verified and call_id and customer_id:
            session = self.database.create_verification(
                call_id=call_id,
                customer_id=customer_id,
                phone=normalize_phone(str(data.get("phone") or data.get("caller_number") or "")),
                method="knowledge_match",
                matches=matches,
                ttl_minutes=int(self.settings.compliance_config.get("verification_ttl_minutes", 30)),
            )
        else:
            session = {}
        return {
            "ok": True,
            "operation": "verify_customer_identity",
            "verified": verified,
            "matched_fields": [key for key, value in matches.items() if value],
            "required_matches": required,
            "verification": session,
            "roomflow": remote.model_dump(),
            "safe_message": (
                "The account has been verified."
                if verified
                else "I could not verify enough account details. I can arrange a callback from Floodman."
            ),
        }

    async def _op_get_billing_summary(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        customer_id = str(data.get("customer_id") or "")
        invoice_id = str(data.get("invoice_id") or "")
        if not customer_id:
            return {"ok": False, "operation": "get_billing_summary", "error": "customer_id_required"}
        invoice = self.database.get_invoice(invoice_id=invoice_id) if invoice_id else None
        if invoice and str(invoice.get("customer_id") or "") != customer_id:
            return {
                "ok": False,
                "operation": "get_billing_summary",
                "error": "invoice_customer_mismatch",
                "safe_message": "That invoice does not belong to the verified customer account.",
            }
        invoices = [invoice] if invoice else self.database.list_invoices(customer_id=customer_id, limit=20)
        remote = await self.roomflow.execute(
            "get_billing_summary", data, queue_on_failure=False
        )
        return {
            "ok": bool(invoices) or remote.ok,
            "operation": "get_billing_summary",
            "invoices": [self._safe_invoice(value) for value in invoices if value],
            "roomflow": self._safe_roomflow_status(remote),
        }

    async def _op_send_payment_link(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        customer_id = str(data.get("customer_id") or "")
        invoice_id = str(data.get("invoice_id") or "")
        invoice = self.database.get_invoice(invoice_id=invoice_id)
        if not invoice:
            return {"ok": False, "operation": "send_payment_link", "error": "invoice_not_found"}
        if not customer_id or str(invoice.get("customer_id") or "") != customer_id:
            return {
                "ok": False,
                "operation": "send_payment_link",
                "error": "invoice_customer_mismatch",
                "safe_message": "I cannot send a payment link for an invoice outside the verified customer account.",
            }
        payment_url = str(invoice.get("payment_url") or "")
        if not payment_url:
            return {
                "ok": False,
                "operation": "send_payment_link",
                "error": "payment_link_unavailable",
                "safe_message": "A secure payment link is not available. I can arrange a billing callback.",
            }
        sync = await self._sync(
            "send_payment_link",
            {**data, "payment_url": payment_url},
            idempotency_key or f"payment-link:{invoice_id}:{data.get('channel')}",
        )
        return {
            "ok": sync.ok or sync.queued,
            "operation": "send_payment_link",
            "has_payment_link": True,
            "delivery": self._safe_roomflow_status(sync),
            "delivered": sync.ok,
            "queued": sync.queued,
            "safe_message": (
                "The secure payment link has been sent."
                if sync.ok
                else "I saved a request for the Floodman billing team to send the secure payment link."
            ),
        }

    async def _op_create_callback_task(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        phone = normalize_phone(str(data.get("phone") or data.get("caller_number") or ""))
        task = self.database.create_callback_task(
            {
                **data,
                "phone": phone,
                "metadata": {"source": "voice", **dict(data.get("metadata") or {})},
            }
        )
        sync = await self._sync(
            "create_callback_task",
            {**data, "phone": phone, "local_callback_id": task["id"]},
            idempotency_key or f"callback:{task['id']}",
        )
        outbound_job: dict[str, Any] | None = None
        if bool(data.get("automated_callback")):
            requested_at = datetime.now(timezone.utc)
            scheduled_for = self._parse_datetime(
                str(data.get("preferred_time") or ""), self.settings.timezone
            ) or requested_at + timedelta(minutes=5)
            outbound_job = self.database.create_outbound_job(
                OutboundJobCreate(
                    phone=phone,
                    purpose=OutboundPurpose.REQUESTED_CALLBACK,
                    customer_id=str(data.get("customer_id") or ""),
                    timezone=str(data.get("timezone") or self.settings.timezone),
                    scheduled_for=scheduled_for,
                    requested_at=requested_at,
                    agent=self.settings.callback_agent,
                    payload={"callback_task_id": task["id"], "reason": data.get("reason")},
                ),
                self.settings.callback_agent,
            )
        return {
            "ok": True,
            "operation": "create_callback_task",
            "callback": task,
            "outbound_job": outbound_job,
            "roomflow": sync.model_dump(),
            "queued": sync.queued,
        }

    async def _op_record_call_outcome(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        sync = await self._sync(
            "record_call_outcome",
            data,
            idempotency_key or f"call-outcome:{data.get('call_id') or uuid.uuid4()}",
        )
        return {"ok": True, "operation": "record_call_outcome", "roomflow": sync.model_dump()}

    async def _op_record_opt_out(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        sync = await self._sync(
            "record_opt_out",
            data,
            idempotency_key or f"optout:{data.get('phone')}",
        )
        return {"ok": True, "operation": "record_opt_out", "roomflow": sync.model_dump()}

    async def _op_record_security_event(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        sync = await self._sync(
            "record_security_event",
            data,
            idempotency_key or f"security:{data.get('call_id') or uuid.uuid4()}",
        )
        return {"ok": True, "operation": "record_security_event", "roomflow": sync.model_dump()}

    def _ensure_customer_property(self, data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        customer_id = str(data.get("customer_id") or "")
        customer = self.database.get_customer(customer_id) if customer_id else None
        if not customer:
            customer = self.database.upsert_customer(
                {
                    "id": customer_id,
                    "name": str(data.get("name") or "Unknown caller"),
                    "phone": normalize_phone(str(data.get("phone") or data.get("caller_number") or "")),
                    "email": str(data.get("email") or ""),
                    "source": str(data.get("source") or "voice"),
                    "metadata": {"call_id": data.get("call_id")},
                }
            )
        address = str(data.get("address") or "").strip()
        property_row: dict[str, Any] = {}
        if address:
            property_row = self.database.upsert_property(
                {
                    "customer_id": customer["id"],
                    "address": address,
                    "city": str(data.get("city") or ""),
                    "state": str(data.get("state") or "MI"),
                    "zip": str(data.get("zip") or ""),
                    "notes": str(data.get("access_notes") or ""),
                    "metadata": {"call_id": data.get("call_id")},
                }
            )
        return customer, property_row

    def _ingest_remote_customer(self, data: dict[str, Any]) -> None:
        candidates: list[dict[str, Any]] = []
        if isinstance(data.get("customer"), dict):
            candidates.append(data["customer"])
        raw = data.get("customers") or data.get("results")
        if isinstance(raw, list):
            candidates.extend(value for value in raw if isinstance(value, dict))
        for value in candidates:
            name = value.get("name") or value.get("displayName") or value.get("full_name")
            phone = value.get("phone") or value.get("phoneNumber") or value.get("mobile")
            if not name:
                continue
            customer = self.database.upsert_customer(
                {
                    "external_id": value.get("id") or value.get("customer_id") or "",
                    "name": str(name),
                    "phone": normalize_phone(str(phone or "")),
                    "email": str(value.get("email") or ""),
                    "source": "roomflow",
                    "metadata": value,
                }
            )
            properties = value.get("properties") or value.get("locations") or []
            if isinstance(properties, list):
                for prop in properties:
                    if not isinstance(prop, dict):
                        continue
                    address = prop.get("address") or prop.get("street") or prop.get("formatted_address")
                    if address:
                        self.database.upsert_property(
                            {
                                "external_id": prop.get("id") or "",
                                "customer_id": customer["id"],
                                "address": str(address),
                                "city": str(prop.get("city") or ""),
                                "state": str(prop.get("state") or "MI"),
                                "zip": str(prop.get("zip") or prop.get("postal_code") or ""),
                                "metadata": prop,
                            }
                        )

    @staticmethod
    def _mask_name(value: str) -> str:
        parts = [part for part in re.split(r"\s+", value.strip()) if part]
        return " ".join(part[0] + "*" * max(1, len(part) - 1) for part in parts)

    @classmethod
    def _safe_customer_candidate(cls, customer: dict[str, Any]) -> dict[str, Any]:
        phone = normalize_phone(str(customer.get("phone") or ""))
        return {
            "customer_id": customer.get("id"),
            "name_masked": cls._mask_name(str(customer.get("name") or "Customer")),
            "phone_last4": phone[-4:] if len(phone) >= 4 else "",
            "property_count": len(customer.get("properties") or []),
            "source": customer.get("source") or "local",
        }

    @staticmethod
    def _safe_roomflow_status(result: RoomflowResult) -> dict[str, Any]:
        return {
            "ok": result.ok,
            "operation": result.operation,
            "status_code": result.status_code,
            "error": result.error,
            "queued": result.queued,
            "outbox_id": result.outbox_id,
        }

    @staticmethod
    def _identity_matches(bundle: dict[str, Any], data: dict[str, Any]) -> dict[str, bool]:
        customer_phone = normalize_phone(str(bundle.get("phone") or ""))
        supplied_phone = normalize_phone(str(data.get("phone") or data.get("caller_number") or ""))
        supplied_name = re.sub(r"\s+", " ", str(data.get("name") or "").lower()).strip()
        customer_name = re.sub(r"\s+", " ", str(bundle.get("name") or "").lower()).strip()
        supplied_street = re.sub(r"\D", "", str(data.get("street_number") or ""))
        supplied_zip = re.sub(r"\D", "", str(data.get("zip") or ""))[:5]
        properties = bundle.get("properties") or []
        property_streets: set[str] = set()
        for prop in properties:
            match = re.match(r"\s*(\d+)", str(prop.get("address") or ""))
            if match:
                property_streets.add(match.group(1))
        property_zips = {re.sub(r"\D", "", str(prop.get("zip") or ""))[:5] for prop in properties}
        return {
            "phone": bool(supplied_phone and customer_phone and supplied_phone == customer_phone),
            "name": bool(
                supplied_name
                and customer_name
                and (supplied_name == customer_name or supplied_name in customer_name)
            ),
            "street_number": bool(supplied_street and supplied_street in property_streets),
            "zip": bool(supplied_zip and supplied_zip in property_zips),
        }

    @staticmethod
    def _safe_invoice(invoice: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": invoice.get("id"),
            "invoice_number": invoice.get("invoice_number"),
            "status": invoice.get("status"),
            "amount_due": invoice.get("amount_due"),
            "currency": invoice.get("currency"),
            "due_date": invoice.get("due_date"),
            "has_payment_link": bool(invoice.get("payment_url")),
        }

    @staticmethod
    def _emergency_summary(data: dict[str, Any]) -> str:
        parts = [
            str(data.get("water_status") or "water condition unknown"),
            str(data.get("hazard") or "no hazard reported"),
            str(data.get("probable_source") or "source unknown"),
            str(data.get("affected_area") or "affected area unknown"),
        ]
        return "; ".join(parts)

    @staticmethod
    def _parse_datetime(value: str, timezone_name: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                from zoneinfo import ZoneInfo

                parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
            return parsed.astimezone(timezone.utc)
        except (ValueError, KeyError):
            return None
