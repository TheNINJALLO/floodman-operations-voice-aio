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
from app.intake import (
    classify_service_request,
    flatten_transcript,
    has_meaningful_intake,
    infer_partial_status,
    intake_missing_fields,
    next_intake_question,
    merge_intake_snapshot,
    normalize_service_status,
    transcript_excerpt,
)
from app.intake_flow import (
    contact_confirmations,
    intake_submission_missing_fields,
    next_intake_state,
    update_confirmation_metadata,
)
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

    async def _op_classify_service(
        self,
        data: dict[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Compatibility endpoint for internal callers.

        The inbound voice agent no longer calls this operation directly. Service
        classification is performed silently inside capture_intake_progress so a
        category label can never become a standalone spoken turn.
        """

        requested = str(
            data.get("requested_service")
            or data.get("service_requested")
            or data.get("service")
            or ""
        ).strip()
        description = str(
            data.get("description")
            or data.get("problem")
            or ""
        ).strip()

        capture = await self._op_capture_intake_progress(
            {
                **data,
                "service_requested": requested,
                "description": description,
            },
            idempotency_key=idempotency_key,
        )

        call_id = str(data.get("call_id") or "").strip()
        stored = (
            self.database.get_call_intake(call_id) or {}
            if call_id
            else {}
        )
        return {
            **capture,
            "operation": "classify_service",
            "service_status": normalize_service_status(
                stored.get("service_status")
            ),
            "service_key": str(stored.get("service_key") or ""),
        }

    async def _op_capture_intake_progress(
        self,
        data: dict[str, Any],
        *,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Persist one caller turn and return one exact next action.

        Service classification is server-owned and silent. The response deliberately
        omits category labels, missing-field lists, and classification reasons so
        the voice model has only one useful sentence to speak.
        """

        call_id = str(data.get("call_id") or "").strip()
        if not call_id:
            return {
                "ok": False,
                "operation": "capture_intake_progress",
                "error": "call_id_required",
            }

        existing = self.database.get_call_intake(call_id) or {}

        requested_phone = normalize_phone(
            str(data.get("phone") or "")
        )
        caller_phone = normalize_phone(
            str(
                data.get("caller_number")
                or existing.get("caller_number")
                or ""
            )
        )
        existing_phone = normalize_phone(
            str(existing.get("phone") or "")
        )
        if re.fullmatch(
            r"\+[1-9][0-9]{7,14}",
            requested_phone,
        ):
            phone = requested_phone
        elif re.fullmatch(
            r"\+[1-9][0-9]{7,14}",
            existing_phone,
        ):
            phone = existing_phone
        else:
            phone = caller_phone

        update = {
            **data,
            "direction": "inbound",
            "caller_number": caller_phone,
            "phone": phone,
            "status": str(data.get("status") or "collecting"),
        }

        # Classification fields are controlled by the server, never by the LLM.
        for key in (
            "service_key",
            "service_status",
            "service_reason",
        ):
            update.pop(key, None)

        requested = str(
            data.get("service_requested")
            or existing.get("service_requested")
            or ""
        ).strip()
        description = str(
            data.get("description")
            or data.get("problem")
            or existing.get("description")
            or existing.get("problem")
            or ""
        ).strip()

        raw_existing_status = str(
            existing.get("service_status") or ""
        ).strip().lower()
        classification_new = False

        if (
            raw_existing_status
            not in {"supported", "unsupported", "review"}
            and (requested or description)
        ):
            classification = classify_service_request(
                dict(self.settings.config.get("business") or {}),
                requested,
                description,
            )
            classification_new = True

            metadata = dict(existing.get("metadata") or {})
            incoming_metadata = data.get("metadata")
            if isinstance(incoming_metadata, dict):
                metadata.update(incoming_metadata)
            metadata["classification_source"] = "server"
            metadata["service_questions"] = list(
                classification.get("intake_questions") or ()
            )

            update.update(
                {
                    "service_requested": requested,
                    "service_key": (
                        classification.get("service_key") or ""
                    ),
                    "service_status": (
                        classification.get("service_status")
                        or "review"
                    ),
                    "service_reason": (
                        classification.get("service_reason") or ""
                    ),
                    "metadata": metadata,
                }
            )

        row = self.database.upsert_call_intake(call_id, update)

        metadata = update_confirmation_metadata(
            existing,
            row,
            confirm_field=data.get("confirm_field") or "",
            confirmation=data.get("confirmation") or "",
        )
        row = self.database.upsert_call_intake(
            call_id,
            {"metadata": metadata},
        )

        service_questions = (
            dict(row.get("metadata") or {}).get(
                "service_questions"
            )
            or ()
        )
        state = next_intake_state(
            row,
            service_questions=service_questions,
        )
        missing = list(state["missing"])
        status = normalize_service_status(
            row.get("service_status")
        )

        prefix = ""
        if classification_new and status == "unsupported":
            prefix = (
                "That is not a service Floodman offers, but I will "
                "still send the details to the team."
            )
        elif classification_new and status == "review":
            prefix = "I will have the team review it."

        safe_message = " ".join(
            part
            for part in (
                prefix,
                state["safe_message"],
            )
            if part
        ).strip()

        submitted = False
        submission_result: dict[str, Any] = {}
        if state["ready_to_submit"]:
            submission_result = await self._op_submit_intake(
                {
                    "call_id": call_id,
                    "caller_number": caller_phone,
                },
                idempotency_key=f"intake-{call_id}",
            )
            submitted = bool(submission_result.get("ok"))
            safe_message = str(
                submission_result.get("safe_message") or ""
            ).strip()
            if not safe_message:
                safe_message = (
                    "You're all set. The team has your information "
                    f"and will call you within "
                    f"{self.settings.callback_sla_hours} hours. "
                    "Thanks for calling Floodman."
                )

        submit_required = False

        self.database.add_call_event(
            call_id,
            "inbound",
            "intake_progress_saved",
            {
                "known_fields": sorted(
                    key
                    for key in (
                        "name",
                        "phone",
                        "email",
                        "address",
                        "service_requested",
                        "description",
                        "property_context",
                        "safety_summary",
                        "timing_summary",
                        "insurance_summary",
                        "evidence_summary",
                    )
                    if row.get(key)
                ),
                "confirmed_fields": sorted(
                    contact_confirmations(row)
                ),
                "missing": missing,
                "stage": (
                    "complete" if submitted else state["stage"]
                ),
                "next_field": state["field"],
                "next_question": state["next_question"],
                "service_status": status,
                "classification_new": classification_new,
                "submit_required": submit_required,
                "submitted": submitted,
            },
        )

        # Deliberately return only conversational control fields. Internal service
        # labels stay in the database and never become something Ava can announce.
        return {
            "ok": True,
            "operation": "capture_intake_progress",
            "saved": True,
            "ready_to_submit": state["ready_to_submit"],
            "submit_required": submit_required,
            "submitted": submitted,
            "continuation_required": (
                not state["ready_to_submit"] or not submitted
            ),
            "stage": (
                "complete" if submitted else state["stage"]
            ),
            "next_field": state["field"],
            "confirmation_required": state[
                "confirmation_required"
            ],
            "next_question": (
                ""
                if submitted
                else state["next_question"]
            ),
            "safe_message": safe_message,
            "end_call_after_message": submitted,
            "speak_verbatim": True,
        }

    async def _op_submit_intake(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        # Finalize the durable snapshot already saved during the conversation.
        call_id = str(data.get("call_id") or "").strip()
        if not call_id:
            return {
                "ok": False,
                "operation": "submit_intake",
                "error": "call_id_required",
                "safe_message": "I saved the information, but the final call record needs operator review.",
            }
        stored = self.database.get_call_intake(call_id) or {}
        snapshot = merge_intake_snapshot(stored, data)

        requested_phone = normalize_phone(str(snapshot.get("phone") or ""))
        caller_phone = normalize_phone(
            str(snapshot.get("caller_number") or data.get("caller_number") or "")
        )
        phone = (
            requested_phone
            if re.fullmatch(r"\+[1-9][0-9]{7,14}", requested_phone)
            else caller_phone
        )
        snapshot["phone"] = phone
        snapshot["caller_number"] = caller_phone
        snapshot["direction"] = "inbound"
        snapshot["department"] = normalize_department(
            str(snapshot.get("department") or "estimating")
        )
        snapshot["urgency"] = str(snapshot.get("urgency") or "normal").strip().lower()

        if normalize_service_status(snapshot.get("service_status")) not in {
            "supported",
            "unsupported",
            "review",
        }:
            service_result = classify_service_request(
                dict(self.settings.config.get("business") or {}),
                snapshot.get("service_requested") or "",
                snapshot.get("description") or "",
            )
            snapshot.update(
                {
                    "service_key": service_result.get("service_key") or "",
                    "service_status": service_result.get("service_status") or "review",
                    "service_reason": service_result.get("service_reason") or "",
                }
            )

        flow_state = next_intake_state(snapshot)
        missing = list(flow_state["missing"])
        self.database.upsert_call_intake(
            call_id,
            {
                **snapshot,
                "status": "collecting" if missing else snapshot.get("status") or "collecting",
            },
        )
        if missing:
            return {
                "ok": False,
                "operation": "submit_intake",
                "error": "required_intake_fields_missing",
                "missing": missing,
                "next_question": flow_state["next_question"],
                "safe_message": flow_state["safe_message"],
            }

        service_status = normalize_service_status(snapshot.get("service_status")) or "review"
        requested_label = str(snapshot.get("service_requested") or "that service").strip()

        def caller_safe_message() -> str:
            hours = self.settings.callback_sla_hours
            if service_status == "unsupported":
                return (
                    "Floodman does not currently offer that service. "
                    "I have still sent your information to the team. "
                    f"They will call you within {hours} hours. "
                    "Thanks for calling Floodman."
                )
            if service_status == "review":
                return (
                    "I have sent your information to the team for "
                    f"review. They will call you within {hours} hours. "
                    "Thanks for calling Floodman."
                )
            if snapshot["urgency"] == "emergency":
                return (
                    "You're all set. I have alerted the emergency "
                    "team. Someone will call you as soon as possible. "
                    "Thanks for calling Floodman."
                )
            return (
                "You're all set. The team has your information and "
                f"will call you within {hours} hours. "
                "Thanks for calling Floodman."
            )

        # Tool retries and model duplicates must not create a second lead or
        # callback. Return the already-completed record and the same caller-safe
        # message when this call was finalized previously.
        if (
            stored.get("status") in {"complete", "unsupported", "review"}
            and stored.get("lead_id")
            and stored.get("callback_id")
        ):
            return {
                "ok": True,
                "operation": "submit_intake",
                "deduplicated": True,
                "intake": stored,
                "department": stored.get("department") or snapshot["department"],
                "service_status": normalize_service_status(
                    stored.get("service_status")
                ) or service_status,
                "notification_ids": list(stored.get("notification_ids") or []),
                "notification_count": int(stored.get("notification_count") or 0),
                "safe_message": caller_safe_message(),
            }

        final_status = (
            "unsupported"
            if service_status == "unsupported"
            else "complete"
            if service_status == "supported"
            else "review"
        )
        snapshot["status"] = final_status
        snapshot["completed_at"] = datetime.now(timezone.utc).isoformat()
        snapshot["source"] = "voice"
        snapshot["problem"] = snapshot.get("description") or ""
        snapshot["service"] = (
            snapshot.get("service_key")
            or snapshot.get("service_requested")
            or "property service request"
        )
        snapshot["email"] = str(snapshot.get("email") or "")

        customer, property_row = self._ensure_customer_property(snapshot)
        lead = self.database.create_lead(
            {
                "customer_id": customer["id"],
                "property_id": property_row.get("id") or "",
                "service": snapshot["service"],
                "problem": snapshot["description"],
                "urgency": snapshot["urgency"],
                "status": "new",
                "source": "voice",
                "metadata": {
                    "call_id": call_id,
                    "department": snapshot["department"],
                    "service_status": service_status,
                    "service_reason": snapshot.get("service_reason") or "",
                    "property_context": snapshot.get("property_context") or "",
                    "safety_summary": snapshot.get("safety_summary") or "",
                    "timing_summary": snapshot.get("timing_summary") or "",
                    "insurance_summary": snapshot.get("insurance_summary") or "",
                    "evidence_summary": snapshot.get("evidence_summary") or "",
                },
            }
        )
        callback = self.database.create_callback_task(
            {
                "customer_id": customer["id"],
                "call_id": call_id,
                "name": snapshot["name"],
                "phone": phone,
                "department": snapshot["department"],
                "reason": snapshot["description"],
                "urgency": snapshot["urgency"],
                "preferred_time": (
                    "immediate"
                    if snapshot["urgency"] == "emergency"
                    else str(
                        snapshot.get("timing_summary")
                        or f"within {self.settings.callback_sla_hours} hours"
                    )
                ),
                "metadata": {
                    "lead_id": lead["id"],
                    "property_id": property_row.get("id") or "",
                    "address": snapshot["address"],
                    "service": snapshot["service"],
                    "service_status": service_status,
                    "source": "voice",
                },
            }
        )

        roomflow_outbox_id = ""
        if self.settings.roomflow_enabled:
            roomflow_outbox_id = self.database.queue_outbox(
                "create_lead",
                {
                    **snapshot,
                    "local_customer_id": customer["id"],
                    "local_property_id": property_row.get("id") or "",
                    "local_lead_id": lead["id"],
                    "local_callback_id": callback["id"],
                },
                idempotency_key or f"intake-roomflow:{call_id}",
            )

        notification_ids: list[str] = []
        recipients = team_alert_recipients(self.settings, snapshot["department"])
        if self.settings.team_sms_enabled and recipients:
            body = build_intake_sms(
                {**snapshot, "call_id": call_id},
                self.settings.callback_sla_hours,
            )
            for recipient in recipients:
                notification_ids.append(
                    self.database.queue_outbox(
                        "team_sms_alert",
                        {
                            "to": recipient,
                            "body": body,
                            "department": snapshot["department"],
                            "call_id": call_id,
                            "lead_id": lead["id"],
                            "intake_status": final_status,
                        },
                        f"team-intake-final:{call_id}:{recipient}",
                    )
                )

        saved = self.database.upsert_call_intake(
            call_id,
            {
                **snapshot,
                "customer_id": customer["id"],
                "property_id": property_row.get("id") or "",
                "lead_id": lead["id"],
                "callback_id": callback["id"],
                "notification_ids": notification_ids,
                "notification_count": len(notification_ids),
                "notification_status": "queued" if notification_ids else "not_queued",
            },
        )
        self.database.add_call_event(
            call_id,
            "inbound",
            "intake_submitted",
            {
                "customer_id": customer["id"],
                "property_id": property_row.get("id") or "",
                "lead_id": lead["id"],
                "callback_id": callback["id"],
                "service_status": service_status,
                "status": final_status,
                "notification_count": len(notification_ids),
            },
        )
        return {
            "ok": True,
            "operation": "submit_intake",
            "customer": customer,
            "property": property_row,
            "lead": lead,
            "callback": callback,
            "intake": saved,
            "department": snapshot["department"],
            "service_status": service_status,
            "roomflow_outbox_id": roomflow_outbox_id,
            "notification_ids": notification_ids,
            "notification_count": len(notification_ids),
            "safe_message": caller_safe_message(),
        }

    async def _op_finalize_inbound_intake(
        self, data: dict[str, Any], *, idempotency_key: str = ""
    ) -> dict[str, Any]:
        call_id = str(data.get("call_id") or "").strip()
        if not call_id:
            return {
                "ok": False,
                "operation": "finalize_inbound_intake",
                "error": "call_id_required",
            }

        existing = self.database.get_call_intake(call_id) or {}
        transcript = data.get("transcript") or []
        transcript_text = flatten_transcript(transcript)
        summary = str(data.get("summary") or "").strip()
        outcome = str(data.get("outcome") or "").strip()
        caller_phone = normalize_phone(
            str(data.get("caller_number") or existing.get("caller_number") or "")
        )
        update: dict[str, Any] = {
            "direction": "inbound",
            "caller_number": caller_phone,
            "phone": existing.get("phone") or caller_phone,
            "summary": summary,
            "outcome": outcome,
            "transcript": transcript,
            "transcript_text": transcript_text,
            "metadata": dict(data.get("metadata") or {}),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if not existing.get("description"):
            update["description"] = summary or transcript_excerpt(transcript_text, 1000)
        current_status = str(existing.get("status") or "collecting")
        if current_status not in {"complete", "unsupported", "review"}:
            update["status"] = infer_partial_status(
                outcome,
                summary,
                data.get("metadata"),
            )

        row = self.database.upsert_call_intake(call_id, update)
        if not has_meaningful_intake(row):
            return {
                "ok": True,
                "operation": "finalize_inbound_intake",
                "intake": row,
                "notification_count": int(row.get("notification_count") or 0),
                "skipped": "no_meaningful_caller_information",
            }

        if normalize_service_status(row.get("service_status")) not in {
            "supported",
            "unsupported",
            "review",
        } and row.get("service_requested"):
            classification = classify_service_request(
                dict(self.settings.config.get("business") or {}),
                row.get("service_requested") or "",
                row.get("description") or "",
            )
            row = self.database.upsert_call_intake(
                call_id,
                {
                    "service_key": classification.get("service_key") or "",
                    "service_status": classification.get("service_status") or "review",
                    "service_reason": classification.get("service_reason") or "",
                },
            )

        callback_id = str(row.get("callback_id") or "")
        phone = normalize_phone(str(row.get("phone") or row.get("caller_number") or ""))
        if not callback_id and re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
            callback = self.database.create_callback_task(
                {
                    "call_id": call_id,
                    "name": row.get("name") or "Unknown caller",
                    "phone": phone,
                    "department": normalize_department(
                        str(row.get("department") or "estimating")
                    ),
                    "reason": row.get("description")
                    or "Partial inbound call. Review the transcript.",
                    "urgency": row.get("urgency") or "normal",
                    "preferred_time": f"within {self.settings.callback_sla_hours} hours",
                    "metadata": {
                        "partial_intake": True,
                        "status": row.get("status") or "partial_call_ended",
                        "address": row.get("address") or "",
                        "service": row.get("service_requested") or "",
                    },
                }
            )
            callback_id = str(callback.get("id") or "")
            if self.settings.roomflow_enabled:
                self.database.queue_outbox(
                    "create_callback_task",
                    {
                        "call_id": call_id,
                        "name": row.get("name") or "Unknown caller",
                        "phone": phone,
                        "department": row.get("department") or "estimating",
                        "reason": row.get("description")
                        or "Partial inbound call. Review transcript.",
                        "urgency": row.get("urgency") or "normal",
                        "local_callback_id": callback_id,
                    },
                    f"partial-callback-roomflow:{call_id}",
                )
            row = self.database.upsert_call_intake(
                call_id,
                {"callback_id": callback_id},
            )

        notification_ids = list(row.get("notification_ids") or [])
        if not int(row.get("notification_count") or 0):
            department = normalize_department(
                str(row.get("department") or "estimating")
            )
            recipients = team_alert_recipients(self.settings, department)
            if self.settings.team_sms_enabled and recipients:
                body = build_intake_sms(row, self.settings.callback_sla_hours)
                for recipient in recipients:
                    notification_ids.append(
                        self.database.queue_outbox(
                            "team_sms_alert",
                            {
                                "to": recipient,
                                "body": body,
                                "department": department,
                                "call_id": call_id,
                                "intake_status": row.get("status")
                                or "partial_call_ended",
                            },
                            f"team-intake-final:{call_id}:{recipient}",
                        )
                    )
                row = self.database.upsert_call_intake(
                    call_id,
                    {
                        "notification_ids": notification_ids,
                        "notification_count": len(notification_ids),
                        "notification_status": (
                            "queued" if notification_ids else "not_queued"
                        ),
                    },
                )

        self.database.add_call_event(
            call_id,
            "inbound",
            "intake_recovered_after_call",
            {
                "status": row.get("status") or "partial_call_ended",
                "service_status": row.get("service_status") or "unknown",
                "callback_id": callback_id,
                "notification_count": len(notification_ids),
                "transcript_chars": len(transcript_text),
            },
        )
        return {
            "ok": True,
            "operation": "finalize_inbound_intake",
            "intake": row,
            "notification_count": len(notification_ids),
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
