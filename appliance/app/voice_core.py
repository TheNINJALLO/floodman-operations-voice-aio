from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.business import BusinessDirectory
from app.config import Settings
from app.db import Database
from app.intake import (
    clean,
    classify_service,
    classify_property_context,
    detect_emergency,
    human_requested,
    normalize_confirmation,
    normalize_email,
    normalize_name,
    normalize_phone,
    normalized,
)
from app.intake_flow import collection_question, confirmation_parts, confirmation_question
from app.knowledge import KnowledgeBase
from app.llm import LocalLLM
from app.models import IntakeState, VoiceReply
from app.notifications import TeamNotifier
from app.suite_bridge import BusinessSuiteBridge

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CallSession:
    call_id: int
    state: IntakeState
    no_input_count: int = 0
    notification_sent: bool = False
    interrupted_prompt_stage: str = ""
    interrupted_from_stage: str = ""


class VoiceCore:
    """Purpose-built deterministic receptionist with local-AI extraction."""

    EMAIL_CAPTURE_ATTEMPT_LIMIT = 3
    EMAIL_CONFIRMATION_ATTEMPT_LIMIT = 2

    def __init__(
        self,
        settings: Settings,
        database: Database,
        business: BusinessDirectory,
        knowledge: KnowledgeBase,
        llm: LocalLLM,
        notifier: TeamNotifier,
        suite_bridge: BusinessSuiteBridge | None = None,
    ):
        self.settings = settings
        self.database = database
        self.business = business
        self.knowledge = knowledge
        self.llm = llm
        self.notifier = notifier
        self.suite_bridge = suite_bridge

    def create_session(self, call_uuid: str, caller_number: str = "", called_number: str = "") -> CallSession:
        caller = normalize_phone(caller_number)
        state = IntakeState(
            call_uuid=call_uuid,
            caller_number=caller,
            called_number=normalize_phone(called_number),
            phone=caller,
        )
        call_id = self.database.create_call(state)
        return CallSession(call_id=call_id, state=state)

    async def call_started(self, session: CallSession) -> None:
        if self.suite_bridge is not None:
            self.suite_bridge.queue(session.call_id, session.state, "call-started")
        try:
            await self.notifier.call_started(session.call_id, session.state)
        except Exception:
            # A notification outage cannot delay or terminate the greeting.
            logger.exception("Unable to queue call-started notification for call %s", session.state.call_uuid)

    @staticmethod
    def greeting() -> str:
        return "Hello. This is Alex with Floodman. How may I help you today?"

    def warm_phrases(self) -> tuple[str, ...]:
        """Return the fixed prompts callers are most likely to hear.

        Pre-synthesizing these at startup removes several seconds of generation
        delay from the normal intake path while retaining dynamic confirmation
        playback for the caller's actual details.
        """
        phrases: list[str] = []

        def add(text: str) -> None:
            text = clean(text, 1200)
            if text and text not in phrases:
                phrases.append(text)

        add(self.greeting())
        for stage in (
            "property_context",
            "affected_area",
            "timing_summary",
            "source_summary",
            "safety_summary",
            "email",
            "phone",
            "address",
        ):
            add(collection_question(IntakeState(call_uuid="warm", stage=stage)))
        for service_key in (
            "",
            "mold_remediation",
            "water_damage_restoration",
            "foundation_repair",
            "sump_pump_and_drainage",
        ):
            add(collection_question(IntakeState(call_uuid="warm", stage="timing_summary", service_key=service_key)))
        add("I can get these details to the right team. What name should I put this under?")
        add("Is that correct?")
        add("Sorry, I cut you off.")
        add("I only caught part of that email. Please continue from where you left off, including at and the domain.")
        add("I'm having trouble hearing the email clearly, so I'll have the team confirm it by phone.")
        add("I couldn't confirm that email, so I'll have the team verify it by phone.")
        add("What's the correct email?")
        add("Are you still there? I can wait a moment.")
        add("I'm still here. Say hello when you're ready.")
        add(f"You're all set. The team has your information and will call you within {self.settings.callback_sla_hours} hours. Thanks for calling Floodman. Goodbye.")
        return tuple(phrases)

    def _save(self, session: CallSession) -> None:
        self.database.save_intake(session.call_id, session.state)

    def _assistant(
        self,
        session: CallSession,
        text: str,
        *,
        speech_parts: tuple[str, ...] = (),
        pause_between_parts_ms: int = 0,
        speech_part_speeds: tuple[float | None, ...] = (),
    ) -> VoiceReply:
        text = clean(text, 1200)
        self.database.add_message(session.call_id, "assistant", text)
        self.database.update_prompt(session.call_id, text)
        self._save(session)
        if self.suite_bridge is not None:
            self.suite_bridge.queue(session.call_id, session.state, "transcript-updated")
        return VoiceReply(
            text=text,
            speech_parts=speech_parts,
            pause_between_parts_ms=max(0, pause_between_parts_ms),
            speech_part_speeds=speech_part_speeds,
        )

    def _confirmation(self, session: CallSession, field: str) -> VoiceReply:
        parts = tuple(part for part in confirmation_parts(session.state, field) if part)
        speeds: tuple[float | None, ...] = ()
        if field == "email" and parts:
            speeds = (self.settings.email_readback_speed,) + (None,) * (len(parts) - 1)
        return self._assistant(
            session,
            confirmation_question(session.state, field),
            speech_parts=parts,
            pause_between_parts_ms=300,
            speech_part_speeds=speeds,
        )

    async def _extract(self, field: str, transcript: str, state: IntakeState) -> str:
        result = await self.llm.extract(field, transcript, state.to_dict())
        value = clean(result.get("value"), 1000) if result else ""
        return value

    def _current_question(self, state: IntakeState) -> str:
        if state.stage.startswith("confirm_"):
            return confirmation_question(state, state.stage.removeprefix("confirm_"))
        return collection_question(state)

    def _advance_to_contact(self, state: IntakeState, field: str) -> str:
        if field == "phone" and state.phone:
            state.stage = "confirm_phone"
            return confirmation_question(state, "phone")
        state.stage = field
        return collection_question(state)

    @staticmethod
    def _question_like(transcript: str) -> bool:
        text = normalized(transcript)
        starters = (
            "what ", "when ", "where ", "who ", "why ", "which ", "how ",
            "do you ", "does floodman ", "can you ", "can i ", "could you ",
            "will you ", "would you ", "should i ", "are you ", "is there ", "is it ",
        )
        return "?" in transcript or text.startswith(starters) or any(
            term in text for term in ("how much", "warranty", "insurance", "free estimate", "serve ")
        )

    @staticmethod
    def _starts_intake(transcript: str) -> bool:
        text = normalized(transcript)
        return any(term in text for term in ("estimate", "inspection", "consultation", "appointment"))

    @staticmethod
    def _email_declined(transcript: str) -> bool:
        text = normalized(transcript)
        return text in {
            "skip", "no email", "none", "dont have one", "don t have one", "do not have one",
            "i dont have email", "i don t have email", "i don t have an email", "i do not have email",
        }

    @staticmethod
    def _email_has_domain_marker(value: str) -> bool:
        words = normalized(value).split()
        return "@" in value or "at" in words

    def _capture_email(self, state: IntakeState, transcript: str) -> str:
        """Prefer a fresh complete address while retaining real split spelling.

        If an earlier fragment already contains a domain, the caller's next
        complete address is a restart and must not be appended to that stale
        fragment. A local-part-only fragment can still be joined to a later
        domain, and domain-first audio can be joined in reverse order.
        """
        fragments = [str(value) for value in state.metadata.get("email_fragments", []) if str(value).strip()]
        previous = clean(" ".join(fragments), 320)
        standalone = normalize_email(transcript)
        if standalone and (not previous or self._email_has_domain_marker(previous)):
            return standalone
        if previous:
            combined = normalize_email(f"{previous} {transcript}")
            if combined:
                return combined
            reverse_combined = normalize_email(f"{transcript} {previous}")
            if reverse_combined:
                return reverse_combined
        return standalone

    def _advance_after_unavailable_email(self, session: CallSession, reason: str) -> VoiceReply:
        state = session.state
        state.email = ""
        state.email_status = "unavailable"
        state.confirmations["email"] = "unavailable"
        state.metadata.pop("email_fragments", None)
        state.metadata.pop("email_capture_attempts", None)
        state.metadata.pop("email_confirmation_attempts", None)
        question = self._advance_to_contact(state, "phone")
        if state.stage == "confirm_phone":
            parts = (reason, *confirmation_parts(state, "phone"))
            return self._assistant(
                session,
                f"{reason} {question}",
                speech_parts=parts,
                pause_between_parts_ms=120,
            )
        return self._assistant(
            session,
            f"{reason} {question}",
            speech_parts=(reason, question),
            pause_between_parts_ms=120,
        )

    @staticmethod
    def _answers_interrupted_prompt(stage: str, transcript: str) -> bool:
        text = normalized(transcript)
        if not text:
            return False
        if stage.startswith("confirm_"):
            return bool(normalize_confirmation(transcript))
        if stage == "property_context":
            return bool(classify_property_context(transcript))
        if stage == "timing_summary":
            return any(term in text for term in (
                "today", "tonight", "yesterday", "morning", "afternoon", "evening",
                "ago", "week", "month", "year", "just started", "right now", "not sure",
            ))
        if stage == "safety_summary":
            return bool(normalize_confirmation(transcript)) or any(term in text for term in (
                "safety", "electrical", "sewage", "gas", "spark", "wire", "unsafe",
                "danger", "concern", "all safe", "nothing like that",
            ))
        if stage == "email":
            return True  # Partial spelling is accumulated across turns below.
        if stage == "phone":
            return len(re.sub(r"\D", "", transcript)) >= 7
        if stage == "address":
            return bool(re.search(r"\d", transcript)) or any(
                term in text for term in ("street", "road", "avenue", "drive", "lane", "court", "highway")
            )
        if stage == "affected_area":
            return any(term in text for term in (
                "basement", "crawl", "bathroom", "bedroom", "kitchen", "hallway",
                "wall", "floor", "ceiling", "room", "foundation", "property",
            )) or len(text.split()) >= 6
        if stage == "source_summary":
            return any(term in text for term in (
                "caused", "cause", "pipe", "valve", "leak", "rain", "storm", "drain",
                "pump", "seep", "overflow", "not sure", "dont know", "do not know",
            ))
        if stage == "name":
            return bool(re.search(r"[A-Za-z]{2}", transcript))
        return stage == "issue"

    @staticmethod
    def _append_interrupted_continuation(state: IntakeState, previous_stage: str, transcript: str) -> None:
        field = {
            "issue": "description",
            "affected_area": "affected_area",
            "timing_summary": "timing_summary",
            "source_summary": "source_summary",
            "safety_summary": "safety_summary",
            "name": "name",
            "address": "address",
        }.get(previous_stage)
        if not field:
            return
        current = str(getattr(state, field) or "")
        combined = clean(f"{current} {transcript}", 1000)
        setattr(state, field, normalize_name(combined) if field == "name" else combined)

    def note_prompt_interrupted(self, session: CallSession, *, previous_stage: str, playback_fraction: float) -> None:
        state = session.state
        if playback_fraction >= 0.65 or previous_stage == state.stage or state.stage in {"done", "complete"}:
            return
        session.interrupted_prompt_stage = state.stage
        session.interrupted_from_stage = previous_stage

    @staticmethod
    def _clear_interrupted_prompt(session: CallSession) -> None:
        session.interrupted_prompt_stage = ""
        session.interrupted_from_stage = ""

    def _recover_interrupted_prompt(self, session: CallSession, transcript: str) -> VoiceReply | None:
        state = session.state
        if session.interrupted_prompt_stage != state.stage:
            self._clear_interrupted_prompt(session)
            return None
        previous_stage = session.interrupted_from_stage
        self._clear_interrupted_prompt(session)
        if self._answers_interrupted_prompt(state.stage, transcript):
            return None
        self._append_interrupted_continuation(state, previous_stage, transcript)
        question = self._current_question(state)
        return self._assistant(
            session,
            f"Sorry, I cut you off. {question}".strip(),
            speech_parts=("Sorry, I cut you off.", question),
            pause_between_parts_ms=120,
        )

    @staticmethod
    def _emergency_context(state: IntakeState, transcript: str) -> str:
        return " ".join(
            value for value in (
                state.description,
                state.affected_area,
                state.timing_summary,
                state.source_summary,
                state.safety_summary,
                transcript,
            ) if value
        )

    async def process(self, session: CallSession, transcript: str) -> VoiceReply:
        state = session.state
        transcript = clean(transcript, 4000)
        self.database.add_message(session.call_id, "caller", transcript)
        session.no_input_count = 0

        if state.urgency != "emergency" and detect_emergency(self._emergency_context(state, transcript)):
            state.urgency = "emergency"
            state.department = "emergency"
            await self._notify(session, kind="emergency", partial=True)
            if self.settings.emergency_transfer_number:
                reply = self._assistant(
                    session,
                    "This sounds like an emergency. Please move away from the hazard. "
                    "I'm connecting you with Floodman's emergency contact now. "
                    "If anyone is in immediate danger, call 911.",
                )
                reply.transfer_number = self.settings.emergency_transfer_number
                return reply
            return self._assistant(
                session,
                f"This sounds urgent. If anyone is in immediate danger, call 911. {self._current_question(state)}".strip(),
            )

        if human_requested(transcript):
            number = self.settings.live_transfer_number
            if number:
                await self._notify(session, kind="human_transfer", partial=True)
                reply = self._assistant(session, "I'll connect you with the Floodman team now.")
                reply.transfer_number = number
                return reply
            return self._assistant(session, "I can't complete a live transfer right now, but I'll send your information to the team for a callback.")

        # Answer from deterministic business rules first, then approved local
        # knowledge. Neither path can perform protected business actions.
        question_like = self._question_like(transcript)
        answer = self.business.direct_answer(transcript) if question_like else ""
        if question_like and not answer:
            context = self.knowledge.context(transcript)
            if context:
                answer = await self.llm.answer(transcript, context)
        if answer:
            if state.stage == "issue" and self._starts_intake(transcript):
                state.description = transcript
                service = classify_service(transcript)
                state.service_status = service["service_status"]
                state.service_key = service["service_key"]
                state.property_context = classify_property_context(transcript)
                state.stage = "affected_area" if state.property_context else "property_context"
            self._clear_interrupted_prompt(session)
            follow_up = self._current_question(state)
            return self._assistant(session, f"{answer} {follow_up}".strip())

        recovered = self._recover_interrupted_prompt(session, transcript)
        if recovered is not None:
            return recovered

        if state.stage == "issue":
            state.description = transcript
            service = classify_service(transcript)
            state.service_status = service["service_status"]
            state.service_key = service["service_key"]
            state.property_context = classify_property_context(transcript)
            state.stage = "affected_area" if state.property_context else "property_context"
            prefix = ""
            if state.service_status == "unsupported":
                state.unsupported_notice_spoken = True
                prefix = "That is not a service Floodman offers, but I will still send the details to the team. "
            return self._assistant(session, prefix + collection_question(state))

        if state.stage == "property_context":
            state.property_context = classify_property_context(transcript)
            if not state.property_context:
                extracted = await self._extract("property_context", transcript, state)
                state.property_context = classify_property_context(extracted)
            if not state.property_context:
                return self._assistant(session, "Was that a home or a business?")
            state.stage = "affected_area"
            return self._assistant(session, collection_question(state))

        if state.stage == "affected_area":
            state.affected_area = transcript
            state.stage = "timing_summary"
            return self._assistant(session, collection_question(state))

        if state.stage == "timing_summary":
            state.timing_summary = transcript
            state.stage = "source_summary"
            return self._assistant(session, collection_question(state))

        if state.stage == "source_summary":
            state.source_summary = transcript
            state.stage = "safety_summary"
            return self._assistant(session, collection_question(state))

        if state.stage == "safety_summary":
            state.safety_summary = transcript
            state.stage = "name"
            return self._assistant(session, "I can get these details to the right team. " + collection_question(state))

        if state.stage == "name":
            state.name = normalize_name(transcript)
            if not state.name:
                return self._assistant(session, "What name should I put this under?")
            state.stage = "confirm_name"
            return self._confirmation(session, "name")

        if state.stage == "confirm_name":
            decision = normalize_confirmation(transcript)
            if decision == "yes":
                state.confirmations["name"] = state.name
                state.stage = "email"
                return self._assistant(session, collection_question(state))
            if decision == "no":
                state.stage = "name"
                state.name = ""
                return self._assistant(session, "What's the correct name?")
            return self._confirmation(session, "name")

        if state.stage == "email":
            if self._email_declined(transcript):
                state.email = ""
                state.email_status = "declined"
                state.metadata.pop("email_fragments", None)
                state.metadata.pop("email_capture_attempts", None)
            else:
                fragments = [str(value) for value in state.metadata.get("email_fragments", []) if str(value).strip()]
                state.email = self._capture_email(state, transcript)
                if not state.email:
                    fragments.append(transcript)
                    state.metadata["email_fragments"] = fragments[-4:]
                    attempts = int(state.metadata.get("email_capture_attempts") or 0) + 1
                    state.metadata["email_capture_attempts"] = attempts
                    if attempts >= self.EMAIL_CAPTURE_ATTEMPT_LIMIT:
                        state.metadata["unconfirmed_email_fragments"] = fragments[-4:]
                        return self._advance_after_unavailable_email(
                            session,
                            "I'm having trouble hearing the email clearly, so I'll have the team confirm it by phone.",
                        )
                    return self._assistant(
                        session,
                        "I only caught part of that email. Please continue from where you left off, including at and the domain.",
                    )
                state.metadata.pop("email_fragments", None)
                state.metadata.pop("email_capture_attempts", None)
                state.email_status = "provided"
            state.stage = "confirm_email"
            return self._confirmation(session, "email")

        if state.stage == "confirm_email":
            decision = normalize_confirmation(transcript)
            if decision == "yes":
                state.confirmations["email"] = state.email or state.email_status
                state.metadata.pop("email_confirmation_attempts", None)
                question = self._advance_to_contact(state, "phone")
                if state.stage == "confirm_phone":
                    return self._confirmation(session, "phone")
                return self._assistant(session, question)
            replacement = normalize_email(transcript)
            if replacement:
                state.email = replacement
                state.email_status = "provided"
                state.metadata.pop("email_fragments", None)
                state.metadata.pop("email_capture_attempts", None)
                state.metadata.pop("email_confirmation_attempts", None)
                return self._confirmation(session, "email")
            if decision == "no":
                state.stage = "email"
                state.email = ""
                state.email_status = ""
                state.metadata.pop("email_fragments", None)
                state.metadata.pop("email_capture_attempts", None)
                state.metadata.pop("email_confirmation_attempts", None)
                return self._assistant(session, "What's the correct email?")
            attempts = int(state.metadata.get("email_confirmation_attempts") or 0) + 1
            state.metadata["email_confirmation_attempts"] = attempts
            if attempts >= self.EMAIL_CONFIRMATION_ATTEMPT_LIMIT:
                if state.email:
                    state.metadata["unconfirmed_email"] = state.email
                return self._advance_after_unavailable_email(
                    session,
                    "I couldn't confirm that email, so I'll have the team verify it by phone.",
                )
            return self._confirmation(session, "email")

        if state.stage == "phone":
            state.phone = normalize_phone(transcript)
            if not state.phone:
                extracted = await self._extract("phone", transcript, state)
                state.phone = normalize_phone(extracted)
            if not state.phone:
                return self._assistant(session, "I didn't get a complete callback number. Please say the ten digits again.")
            state.stage = "confirm_phone"
            return self._confirmation(session, "phone")

        if state.stage == "confirm_phone":
            decision = normalize_confirmation(transcript)
            if decision == "yes":
                state.confirmations["phone"] = state.phone
                state.stage = "address"
                return self._assistant(session, collection_question(state))
            if decision == "no":
                state.stage = "phone"
                state.phone = ""
                return self._assistant(session, "What's the correct callback number?")
            return self._confirmation(session, "phone")

        if state.stage == "address":
            state.address = transcript
            area = self.business.service_area(state.address)
            state.service_area_status = area.status
            state.service_area_city = area.city
            state.stage = "confirm_address"
            return self._confirmation(session, "address")

        if state.stage == "confirm_address":
            decision = normalize_confirmation(transcript)
            if decision == "yes":
                state.confirmations["address"] = state.address
                state.stage = "complete"
                state.completed = True
                await self._notify(session, kind="completed_intake", partial=False)
                state.stage = "done"
                text = f"You're all set. The team has your information and will call you within {self.settings.callback_sla_hours} hours. Thanks for calling Floodman. Goodbye."
                reply = self._assistant(session, text)
                reply.end_call = True
                return reply
            if decision == "no":
                state.stage = "address"
                state.address = ""
                return self._assistant(session, "What's the correct service address?")
            return self._confirmation(session, "address")

        if state.stage == "done":
            reply = self._assistant(session, "The team has your information. Thanks for calling Floodman. Goodbye.")
            reply.end_call = True
            return reply

        state.stage = "issue"
        return self._assistant(session, collection_question(state))

    async def _notify(self, session: CallSession, *, kind: str, partial: bool) -> None:
        try:
            count = await self.notifier.send(session.call_id, session.state, kind=kind, partial=partial)
        except Exception:
            # Notification integrations are deliberately fail-open. The intake is
            # already local, so an email/SMS/push outage must never turn a
            # successful caller conversation into Asterisk's technical fallback.
            logger.exception("Unable to queue team notification for call %s", session.state.call_uuid)
            self.database.save_intake(session.call_id, session.state, "delivery_error")
            return
        session.notification_sent = session.notification_sent or count > 0
        self.database.save_intake(session.call_id, session.state, "queued" if count else "not_configured_or_duplicate")

    async def no_input(self, session: CallSession) -> VoiceReply:
        session.no_input_count += 1
        if session.no_input_count == 1:
            return self._assistant(session, "Are you still there? I can wait a moment.")
        if session.no_input_count == 2:
            return self._assistant(session, "I'm still here. Say hello when you're ready.")
        await self._notify(session, kind="partial_no_input", partial=True)
        reply = self._assistant(session, "I'm still not hearing you, so I'll send the information I have to the team. Please call back when you're ready. Goodbye.")
        reply.end_call = True
        return reply

    async def disconnect(self, session: CallSession, outcome: str = "caller_hangup") -> None:
        if not session.state.completed and not session.notification_sent:
            await self._notify(session, kind="partial_hangup", partial=True)
        self.database.finish_call(session.call_id, outcome)
        if self.suite_bridge is not None:
            event_type = "call-failed" if outcome == "error" else "call-ended"
            failure_reason = "Voice processing failed; local partial intake retained" if event_type == "call-failed" else ""
            self.suite_bridge.queue(
                session.call_id,
                session.state,
                event_type,
                failure_reason=failure_reason,
            )
