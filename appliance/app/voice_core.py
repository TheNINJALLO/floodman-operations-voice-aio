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
    detect_emergency,
    human_requested,
    normalize_confirmation,
    normalize_email,
    normalize_phone,
    normalized,
)
from app.intake_flow import collection_question, confirmation_question
from app.knowledge import KnowledgeBase
from app.llm import LocalLLM
from app.models import IntakeState, VoiceReply
from app.notifications import TeamNotifier

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CallSession:
    call_id: int
    state: IntakeState
    no_input_count: int = 0
    notification_sent: bool = False


class VoiceCore:
    """Purpose-built deterministic receptionist with local-AI extraction."""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        business: BusinessDirectory,
        knowledge: KnowledgeBase,
        llm: LocalLLM,
        notifier: TeamNotifier,
    ):
        self.settings = settings
        self.database = database
        self.business = business
        self.knowledge = knowledge
        self.llm = llm
        self.notifier = notifier

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

    @staticmethod
    def greeting() -> str:
        return "Thanks for calling Floodman. This is Ava, Floodman's automated assistant. How can I help?"

    def _save(self, session: CallSession) -> None:
        self.database.save_intake(session.call_id, session.state)

    def _assistant(self, session: CallSession, text: str) -> VoiceReply:
        text = clean(text, 1200)
        self.database.add_message(session.call_id, "assistant", text)
        self.database.update_prompt(session.call_id, text)
        self._save(session)
        return VoiceReply(text=text)

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

    async def process(self, session: CallSession, transcript: str) -> VoiceReply:
        state = session.state
        transcript = clean(transcript, 4000)
        self.database.add_message(session.call_id, "caller", transcript)
        session.no_input_count = 0

        if human_requested(transcript):
            number = self.settings.live_transfer_number
            if number:
                await self._notify(session, kind="human_transfer", partial=True)
                reply = self._assistant(session, "I'll connect you with the Floodman team now.")
                reply.transfer_number = number
                return reply
            return self._assistant(session, "I can't complete a live transfer right now, but I'll send your information to the team for a callback.")

        # Answer approved company questions without losing the active intake step.
        direct = self.business.direct_answer(transcript)
        question_like = "?" in transcript or any(term in normalized(transcript) for term in ("do you", "can you", "what", "how much", "warranty", "insurance", "serve "))
        if direct and question_like:
            follow_up = self._current_question(state)
            return self._assistant(session, f"{direct} {follow_up}".strip())

        if state.stage == "issue":
            state.description = transcript
            service = classify_service(transcript)
            state.service_status = service["service_status"]
            state.service_key = service["service_key"]
            state.stage = "property_context"
            prefix = ""
            if state.service_status == "unsupported":
                state.unsupported_notice_spoken = True
                prefix = "That is not a service Floodman offers, but I will still send the details to the team. "
            return self._assistant(session, prefix + collection_question(state))

        if state.stage == "property_context":
            text = normalized(transcript)
            if any(term in text for term in ("home", "house", "residential", "my residence")):
                state.property_context = "Residential property"
            elif any(term in text for term in ("business", "commercial", "office", "store", "rental property")):
                state.property_context = "Commercial or managed property"
            else:
                state.property_context = await self._extract("property_context", transcript, state) or transcript
            state.stage = "timing_summary"
            return self._assistant(session, collection_question(state))

        if state.stage == "timing_summary":
            state.timing_summary = await self._extract("timing_summary", transcript, state) or transcript
            state.stage = "safety_summary"
            return self._assistant(session, collection_question(state))

        if state.stage == "safety_summary":
            state.safety_summary = await self._extract("safety_summary", transcript, state) or transcript
            if detect_emergency(transcript + " " + state.description):
                state.urgency = "emergency"
                state.department = "emergency"
                await self._notify(session, kind="emergency", partial=True)
                if self.settings.emergency_transfer_number:
                    reply = self._assistant(session, "For safety, move away from the hazard. I'm connecting you with the emergency Floodman contact now. If anyone is in immediate danger, call 911.")
                    reply.transfer_number = self.settings.emergency_transfer_number
                    return reply
            state.stage = "name"
            return self._assistant(session, collection_question(state))

        if state.stage == "name":
            state.name = await self._extract("name", transcript, state) or transcript
            state.stage = "confirm_name"
            return self._assistant(session, confirmation_question(state, "name"))

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
            return self._assistant(session, f"I heard {state.name}. Is that right?")

        if state.stage == "email":
            text = normalized(transcript)
            if text in {"skip", "no email", "none", "dont have one", "do not have one"} or "skip" in text:
                state.email = ""
                state.email_status = "declined"
            else:
                state.email = normalize_email(transcript)
                if not state.email:
                    extracted = await self._extract("email", transcript, state)
                    state.email = normalize_email(extracted)
                if not state.email:
                    return self._assistant(session, "I didn't get a complete email address. Please say it again slowly, or say skip.")
                state.email_status = "provided"
            state.stage = "confirm_email"
            return self._assistant(session, confirmation_question(state, "email"))

        if state.stage == "confirm_email":
            decision = normalize_confirmation(transcript)
            if decision == "yes":
                state.confirmations["email"] = state.email or state.email_status
                question = self._advance_to_contact(state, "phone")
                return self._assistant(session, question)
            if decision == "no":
                state.stage = "email"
                state.email = ""
                state.email_status = ""
                return self._assistant(session, "What's the correct email? You can say skip.")
            return self._assistant(session, confirmation_question(state, "email"))

        if state.stage == "phone":
            state.phone = normalize_phone(transcript)
            if not state.phone:
                extracted = await self._extract("phone", transcript, state)
                state.phone = normalize_phone(extracted)
            if not state.phone:
                return self._assistant(session, "I didn't get a complete callback number. Please say the ten digits again.")
            state.stage = "confirm_phone"
            return self._assistant(session, confirmation_question(state, "phone"))

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
            return self._assistant(session, confirmation_question(state, "phone"))

        if state.stage == "address":
            state.address = await self._extract("address", transcript, state) or transcript
            area = self.business.service_area(state.address)
            state.service_area_status = area.status
            state.service_area_city = area.city
            state.stage = "confirm_address"
            return self._assistant(session, confirmation_question(state, "address"))

        if state.stage == "confirm_address":
            decision = normalize_confirmation(transcript)
            if decision == "yes":
                state.confirmations["address"] = state.address
                state.stage = "complete"
                state.completed = True
                await self._notify(session, kind="completed_intake", partial=False)
                state.stage = "done"
                text = f"You're all set. The team has your information and will call you within {self.settings.callback_sla_hours} hours. Thanks for calling Floodman."
                reply = self._assistant(session, text)
                reply.end_call = True
                return reply
            if decision == "no":
                state.stage = "address"
                state.address = ""
                return self._assistant(session, "What's the correct service address?")
            return self._assistant(session, confirmation_question(state, "address"))

        if state.stage == "done":
            reply = self._assistant(session, "The team has your information. Thanks for calling Floodman.")
            reply.end_call = True
            return reply

        state.stage = "issue"
        return self._assistant(session, collection_question(state))

    async def _notify(self, session: CallSession, *, kind: str, partial: bool) -> None:
        count = await self.notifier.send(session.call_id, session.state, kind=kind, partial=partial)
        session.notification_sent = session.notification_sent or count > 0
        self.database.save_intake(session.call_id, session.state, "sent" if count else "not_configured_or_duplicate")

    async def no_input(self, session: CallSession) -> VoiceReply:
        session.no_input_count += 1
        if session.no_input_count == 1:
            return self._assistant(session, "I didn't catch that. Please say it once more.")
        await self._notify(session, kind="partial_no_input", partial=True)
        reply = self._assistant(session, "I'm still not hearing you, so I'll send the information I have to the team. Please call back when you're ready. Goodbye.")
        reply.end_call = True
        return reply

    async def disconnect(self, session: CallSession, outcome: str = "caller_hangup") -> None:
        if not session.state.completed and not session.notification_sent:
            await self._notify(session, kind="partial_hangup", partial=True)
        self.database.finish_call(session.call_id, outcome)
