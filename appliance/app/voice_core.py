from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.business import BusinessDirectory
from app.config import Settings
from app.call_style import GREETING, INTRODUCTION, IDENTITY, farewell_body, extra_acknowledgment
from app import company_conversation as company
from app.call_diagnostics import emit_current
from app.conversation import (
    DETAIL_STAGES, QUESTIONS, capture_volunteered, closing_finished, contact_fields, declined,
    detail_stage, hold_requested, identity_question, last_name_answer, memory, name_answer, phone_answer, request_kind, unfinished_turn,
)
from app.db import Database
from app.intake import (
    clean,
    address_answer,
    classify_service,
    classify_property_context,
    detect_emergency,
    detect_flood_emergency,
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
    defer_assistant_recording: bool = False
    introduction_pending: bool = False


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
        briefings=None,
    ):
        self.settings = settings
        self.database = database
        self.business = business
        self.knowledge = knowledge
        self.llm = llm
        self.notifier = notifier
        self.suite_bridge = suite_bridge
        self.briefings = briefings

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
        return GREETING

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
        add(INTRODUCTION)
        for stage in (
            "property_context",
            "affected_area",
            "timing_summary",
            "source_summary",
            "email",
            "phone",
            "address",
            "name",
            "last_name",
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
        add("I can pass that along. May I have your first and last name, please?")
        add("Is that correct?")
        add("Is this the best number to call you back on?")
        add("Sorry, I cut you off.")
        add("I only caught part of that email. Please continue from where you left off, including at and the domain.")
        add("I'm having trouble hearing the email clearly, so I'll have the team confirm it by phone.")
        add("I couldn't confirm that email, so I'll have the team verify it by phone.")
        add("What's the correct email?")
        add("Are you still there? I can wait a moment.")
        add("I'm still here. Say hello when you're ready.")
        add("Go ahead, I'm listening.")
        for index in range(3):
            add(extra_acknowledgment(index))
        add("Thank you. " + QUESTIONS["anything_else"])
        add(farewell_body(self.settings.callback_sla_hours))
        add("You're all set.")
        for text in QUESTIONS.values():
            add(text)
        for text in ("Take your time.", "Hi. What can I help you with?"):
            add(text)
        add("Thanks for calling Floodman. Goodbye.")
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
        if session.introduction_pending:
            session.introduction_pending = False
            intro = INTRODUCTION
            text = f"{intro} {text}"
            if speech_parts:
                speech_parts = (intro, *speech_parts)
                if speech_part_speeds:
                    speech_part_speeds = (None, *speech_part_speeds)
        self._save(session)
        if not session.defer_assistant_recording:
            self.record_assistant(session, text)
        return VoiceReply(
            text=text,
            speech_parts=speech_parts,
            pause_between_parts_ms=max(0, pause_between_parts_ms),
            speech_part_speeds=speech_part_speeds,
        )

    def record_assistant(
        self, session: CallSession, text: str, *, interrupted: bool = False
    ) -> None:
        """Persist only speech that the caller actually received from the agent."""

        text = clean(text, 1200)
        if not text:
            return
        self.database.add_message(
            session.call_id,
            "assistant",
            text,
            interrupted=interrupted,
        )
        self.database.update_prompt(session.call_id, text)
        if self.suite_bridge is not None:
            self.suite_bridge.queue(session.call_id, session.state, "transcript-updated")

    def _confirmation(self, session: CallSession, field: str, *, prefix: str = "", spell_email: bool = True) -> VoiceReply:
        parts = tuple(part for part in confirmation_parts(session.state, field, spell_email=spell_email) if part)
        speeds: tuple[float | None, ...] = ()
        if field == "email" and parts:
            speeds = (self.settings.email_readback_speed,) + (None,) * (len(parts) - 1)
        if prefix:
            parts = (prefix.strip(), *parts)
            if speeds:
                speeds = (None, *speeds)
        return self._assistant(
            session,
            (prefix + confirmation_question(session.state, field, spell_email=spell_email)).strip(),
            speech_parts=parts,
            pause_between_parts_ms=180 if field == "email" else 300,
            speech_part_speeds=speeds,
        )

    def _current_question(self, state: IntakeState) -> str:
        if state.stage == "anything_else" and memory(state).get("wrapup_asked"):
            return ""
        if state.stage.startswith("confirm_"):
            return confirmation_question(state, state.stage.removeprefix("confirm_"))
        return collection_question(state)

    @staticmethod
    def _question_like(transcript: str) -> bool:
        text = normalized(transcript)
        starters = (
            "what ", "when ", "where ", "who ", "why ", "which ", "how ",
            "do you ", "does floodman ", "can you ", "can i ", "could you ",
            "will you ", "would you ", "should i ", "are you ", "is there ", "is it ",
        )
        return company.is_question(transcript) or text.startswith(starters) or any(
            term in text for term in ("how much", "serve ")
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
        return self._next_reply(session, prefix=reason + " ")

    def _planned(self, session: CallSession, text: str) -> VoiceReply:
        # Prepared conversational wording needs no CPU rewrite before playback.
        return self._assistant(session, text, speech_parts=(text,))

    def _name_reply(self, session: CallSession, *, prefix: str = "") -> VoiceReply:
        info = memory(session.state)
        if len(session.state.name.split()) == 1 and not info.get("last_name_asked"):
            info["last_name_asked"] = True
            session.state.stage = "last_name"
            return self._planned(session, prefix + collection_question(session.state))
        session.state.stage = "confirm_name"
        return self._confirmation(session, "name", prefix=prefix)

    def _acknowledge_extra(self, session: CallSession) -> VoiceReply:
        info = memory(session.state)
        index = info.get("wrapup_ack_count", 0)
        info.update(wrapup_ack_count=index + 1, wrapup_followup=True)
        return self._planned(session, extra_acknowledgment(index))

    def _next_reply(self, session: CallSession, *, prefix: str = "") -> VoiceReply:
        state = session.state
        info = memory(state)
        next_detail = "" if info.get("contact_started") or info.get("wrapup_asked") else detail_stage(state)
        if next_detail:
            state.stage = next_detail
            info["asked"].append(next_detail)
            return self._planned(session, prefix + self._current_question(state))
        for field in contact_fields(state):
            confirmation = state.confirmations.get(field)
            if field in info["declined"] or (confirmation and confirmation in {getattr(state, field), "unavailable", "declined"}):
                continue
            if getattr(state, field):
                info["contact_started"] = True
                if field == "name":
                    return self._name_reply(session, prefix=prefix)
                state.stage = f"confirm_{field}"
                return self._confirmation(session, field, prefix=prefix)
            state.stage = field
            if not info.get("contact_started"):
                prefix += "I can pass that along. "
                info["contact_started"] = True
            return self._planned(session, prefix + collection_question(state))
        state.stage = "anything_else"
        if info.get("wrapup_asked"):
            return self._acknowledge_extra(session)
        info["wrapup_asked"] = True
        return self._planned(session, prefix + "Thank you. " + QUESTIONS["anything_else"])

    async def _after_confirmation(self, session: CallSession) -> VoiceReply:
        state = session.state
        info = memory(state)
        pending = any(field not in info["declined"] and not (state.confirmations.get(field) and state.confirmations[field] in
                      {getattr(state, field), "unavailable", "declined"})
                      for field in contact_fields(state))
        if info.get("finish_after_confirmation") and not pending:
            return await self._finish_intake(session)
        return self._next_reply(session)

    def _unavailable_contact(self, session: CallSession, field: str) -> VoiceReply:
        state = session.state
        state.confirmations[field] = "unavailable"
        if getattr(state, field):
            state.metadata["unconfirmed_" + field] = getattr(state, field)
            setattr(state, field, "")
        if field == "address":
            state.service_area_status, state.service_area_city = "unknown", ""
        return self._next_reply(session, prefix="I'll leave that for the team to confirm. ")

    def _retry_contact(self, session: CallSession, field: str, question: str) -> VoiceReply:
        attempts = memory(session.state).setdefault("contact_attempts", {})
        attempts[field] = attempts.get(field, 0) + 1
        if attempts[field] >= 2:
            return self._unavailable_contact(session, field)
        return self._planned(session, question)

    async def _finish_intake(self, session: CallSession) -> VoiceReply:
        state = session.state
        required = [field for field in contact_fields(state) if field != "email"]
        state.completed = all(getattr(state, field) and state.confirmations.get(field) == getattr(state, field) for field in required)
        state.stage = "done"
        if session.defer_assistant_recording:
            memory(state)["completion_pending"] = True
            self._save(session)
        else:
            await self._notify(session, kind="completed_intake", partial=not state.completed)
        first_name = state.name.split()[0] if state.name and re.fullmatch(r"[A-Za-z][A-Za-z'-]*", state.name.split()[0]) else ""
        opening = f"You're all set, {first_name}." if first_name else "You're all set."
        body = farewell_body(self.settings.callback_sla_hours)
        reply = self._assistant(session, f"{opening} {body}", speech_parts=(opening, body), pause_between_parts_ms=120)
        reply.end_call = True
        return reply

    async def finalize_completion(self, session: CallSession) -> None:
        if session.state.stage == "done" and memory(session.state).pop("completion_pending", False):
            await self._notify(session, kind="completed_intake", partial=not session.state.completed)

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
        if stage == "water_condition":
            return any(term in text for term in ("water", "damp", "humidity", "moisture", "dry", "wet"))
        if stage == "service_detail":
            return len(text.split()) >= 3
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
        if stage in {"name", "last_name"}:
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
            "water_condition": "affected_area",
            "service_detail": "description",
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

    async def process(
        self,
        session: CallSession,
        transcript: str,
        *,
        confidence: float | None = None,
        language: str = "",
    ) -> VoiceReply:
        state = session.state
        transcript = clean(transcript, 4000)
        self.database.add_message(
            session.call_id,
            "caller",
            transcript,
            confidence=confidence,
            language=language,
        )
        session.no_input_count = 0
        info = memory(state)
        info.pop("hold_timeouts", None)
        if info.get("speech_fragments") and not unfinished_turn(transcript) and not hold_requested(transcript) and not identity_question(transcript):
            fragments = info.pop("speech_fragments")
            if normalized(transcript) not in {"no", "nope", "nothing else", "that s all", "that is all", "no thanks", "no that s all"}:
                # Reassemble before routing so "water is ... rising" retains
                # the same deterministic emergency decision as a single turn.
                transcript = clean(" ".join((*[part.rstrip(".\u2026- ") for part in fragments], transcript)), 4000)
        if state.stage == "done":
            # A caller may interrupt the farewell with a correction. The phone
            # driver keeps the channel open and this resumes message collection.
            state.stage = "anything_else"
            state.completed = False
            info.pop("finish_after_confirmation", None)
            info.pop("completion_pending", None)

        if identity_question(transcript):
            return self._planned(session, (IDENTITY + " " + self._current_question(state)).strip())

        if detect_flood_emergency(transcript, context=self._emergency_context(state, "")):
            state.urgency = "emergency"
            state.department = "emergency"
            state.description = clean(f"{state.description} Urgent update: {transcript}" if state.description else transcript, 4000)
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

        if state.urgency != "emergency" and detect_emergency(transcript) and re.search(
            r"\b(?:electrical|sparks?|sparking|gas|sewage|collapse|collapsing|unsafe|danger)\b", transcript, re.I
        ):
            state.urgency = "emergency"
            state.description = clean(f"{state.description} Urgent update: {transcript}", 4000)
            await self._notify(session, kind="emergency", partial=True)
            return self._planned(session, "If anyone is in immediate danger, call 911. I'll flag this for the team. " + self._current_question(state))

        if human_requested(transcript):
            if state.stage == "issue":
                info["kind"] = "message"
                state.description = transcript
                state.stage = "service_detail"
            return self._planned(session, "I can take a message and ask the team to call you back. " + self._current_question(state))

        if hold_requested(transcript):
            info["hold_timeouts"] = 2
            return self._planned(session, "Take your time.")
        if normalized(transcript) in {"can i ask a question", "can i ask you a question", "may i ask a question", "i have a question", "can i ask about the company"}:
            return self._planned(session, "Of course. What would you like to know?")
        if state.stage == "issue" and normalized(transcript) in {"can you help", "can you help me", "i need help"}:
            return self._planned(session, "Of course. What's happening?")
        if state.stage == "issue" and normalized(transcript) in {"hi", "hello", "hey", "good morning", "good afternoon", "yes", "yes ok", "yes okay", "i m here", "i am here"}:
            return self._planned(session, "Hi. What can I help you with?")
        if unfinished_turn(transcript):
            if normalized(transcript) and normalized(transcript) not in {"um", "uh", "erm", "hmm", "oh", "well", "so", "let me think"}:
                fragments = info.setdefault("speech_fragments", [])
                fragments.append(transcript)
                info["speech_fragments"] = fragments[-8:]
            self._save(session)
            return VoiceReply(text="", wait_for_caller=True)
        if state.stage in {"anything_else", "additional_message"}:
            info.update(wrapup_asked=True, wrapup_followup=True)
        if state.stage in {"anything_else", "additional_message"} and closing_finished(transcript):
            return await self._finish_intake(session)
        if state.stage.removeprefix("confirm_") in {"name", "last_name", "phone", "address", "email"}:
            info["contact_started"] = True
        kind = request_kind(transcript)
        if kind != "service" and not info.get("contact_started"):
            info["kind"] = kind

        repeat_requested = normalized(transcript) in {"what was that", "say that again", "could you repeat that", "i didn t hear you", "i can t hear you"}
        spell_email = normalized(transcript) in {
            "spell it", "spell it out", "spell that", "spell that out", "please spell it", "please spell it out",
            "can you spell it", "can you spell it out", "could you spell it", "could you spell that out",
            "can you spell the email", "can you spell the email out", "spell the email", "spell the email out",
        }
        spelling_requested = state.stage == "confirm_email" and spell_email
        repeat_requested = repeat_requested or spelling_requested
        spell_email = True
        if state.stage != "confirm_email" and repeat_requested:
            return self._planned(session, self._current_question(state) or "Go ahead, I'm listening.")
        before_contacts = tuple(getattr(state, field) for field in ("name", "phone", "address", "email"))
        dialogue = info.setdefault("company_dialogue", {})
        question_like = (self._question_like(transcript) or company.is_question(transcript, has_topic=bool(dialogue))) and not spelling_requested
        if not question_like or self._starts_intake(transcript) or re.search(r"\b(my|our|we have|I have)\b", transcript, re.I):
            capture_volunteered(state, transcript, expected=state.stage)
        if state.address and not state.service_area_city:
            area = self.business.service_area(state.address)
            state.service_area_status, state.service_area_city = area.status, area.city
        if state.stage in {"anything_else", "additional_message"} and before_contacts != tuple(getattr(state, field) for field in ("name", "phone", "address", "email")):
            info["finish_after_confirmation"] = True
            return self._next_reply(session)

        field = state.stage.removeprefix("confirm_")
        negative_confirmation = state.stage.startswith("confirm_") and normalized(transcript) in {
            "no", "nope", "nah", "none", "not sure", "i don t know", "i do not know",
        }
        if field in {"name", "phone", "address", "email"} and declined(transcript) and not negative_confirmation:
            if field == "email":
                if state.email:
                    state.metadata["unconfirmed_email"] = state.email
                state.email = ""
                state.email_status = "declined"
            if field not in info["declined"]:
                info["declined"].append(field)
            return self._next_reply(session)

        # Answer from deterministic business rules first, then approved local
        # knowledge. Neither path can perform protected business actions.
        eligible = question_like and not (state.stage == "issue" and info["kind"] != "service")
        intake_request = self._starts_intake(transcript) and bool(re.search(
            r"\b(?:i need|i want|i d like|i would like|please arrange|can i get|can you arrange|can i talk to (?:someone|somebody)|can i speak to (?:someone|somebody))\b", normalized(transcript)))
        unsupported_question = classify_service(transcript).get("service_status") == "unsupported"
        answer = self.business.direct_answer(transcript) if eligible and (unsupported_question or company.policy_question(transcript) or not company.needs_detail(transcript)) else ""
        query = company.search_query(transcript, dialogue, state.description)
        documents = self.knowledge.search(query, top_k=3, max_chars=3600) if eligible else []
        source = "approved_rules" if answer else "unavailable"
        if question_like and not answer and not (state.stage == "issue" and info["kind"] != "service"):
            context = "\n\n".join(f"# {document.title}\n{document.body}" for document in documents)
            if context:
                contextual = getattr(self.llm, "answer_in_conversation", None)
                if contextual is not None:
                    answer = await contextual(transcript, context, company.context_summary(dialogue, state.description))
                else:
                    answer = await self.llm.answer(transcript, context)
                source = "local_model" if answer else "unavailable"
        if answer:
            company.remember(dialogue, transcript, answer, documents[0].category if documents else classify_service(transcript).get("service_key", ""))
            emit_current("company_answer", reason=source, stage=state.stage)
            if state.stage == "issue" and intake_request:
                state.description = transcript
                service = classify_service(transcript)
                state.service_status = service["service_status"]
                state.service_key = service["service_key"]
                capture_volunteered(state, transcript)
                self._clear_interrupted_prompt(session)
                return self._next_reply(session, prefix=answer + " ")
            self._clear_interrupted_prompt(session)
            return self._planned(session, company.supportive_opening(transcript) + answer)

        if question_like and not repeat_requested and not intake_request and company.question_words(transcript) and not (state.stage == "issue" and info["kind"] != "service"):
            emit_current("company_answer", reason="unavailable", stage=state.stage)
            return self._planned(session, "The team will need to confirm that detail for you.")

        if state.stage == "anything_else":
            closing = normalized(transcript)
            if closing in {"yes", "yeah", "yep", "sure"}:
                state.stage = "additional_message"
                return self._planned(session, QUESTIONS["additional_message"])
            state.description = clean(f"{state.description} Additional message: {transcript}", 4000)
            return self._acknowledge_extra(session)
        if state.stage == "additional_message":
            state.description = clean(f"{state.description} Additional message: {transcript}", 4000)
            state.stage = "anything_else"
            return self._acknowledge_extra(session)

        recovered = self._recover_interrupted_prompt(session, transcript)
        if recovered is not None:
            return recovered

        if state.stage == "issue":
            state.description = transcript
            service = classify_service(transcript)
            state.service_status = service["service_status"]
            state.service_key = service["service_key"]
            capture_volunteered(state, transcript)
            prefix = ""
            if state.service_status == "unsupported":
                state.unsupported_notice_spoken = True
                prefix = "That is not a service Floodman offers, but I will still send the details to the team. "
            prefix = company.supportive_opening(transcript) + prefix
            return self._next_reply(session, prefix=prefix)

        if state.stage == "property_context":
            state.property_context = classify_property_context(transcript)
            return self._next_reply(session)

        if state.stage in DETAIL_STAGES:
            if state.stage not in info["asked"]:
                info["asked"].append(state.stage)
            if state.stage == "water_condition":
                info["water_condition"] = transcript
                state.affected_area = clean(f"{state.affected_area}; {transcript}", 1000).strip("; ")
            elif state.stage == "service_detail":
                state.description = clean(f"{state.description} {transcript}", 4000)
                service = classify_service(state.description)
                state.service_status, state.service_key = service["service_status"], service["service_key"]
            else:
                setattr(state, state.stage, transcript)
            return self._next_reply(session)

        if state.stage == "name":
            state.name = name_answer(transcript)
            if not state.name:
                return self._assistant(session, collection_question(state))
            return self._name_reply(session)

        if state.stage == "last_name":
            surname = "" if declined(transcript) else last_name_answer(transcript)
            if surname and normalized(surname) != normalized(state.name):
                state.name = surname if normalized(surname).startswith(normalized(state.name) + " ") else clean(f"{state.name} {surname}", 120)
            else:
                info["last_name_unavailable"] = True
            return self._name_reply(session)

        if state.stage == "confirm_name":
            decision = normalize_confirmation(transcript)
            if decision == "yes":
                state.confirmations["name"] = state.name
                return await self._after_confirmation(session)
            if decision == "no":
                replacement = re.sub(r"^no[,\s]+(?:(?:it is|it's|my name is)\s+)?", "", transcript, flags=re.I)
                if replacement != transcript and re.fullmatch(r"[A-Za-z][A-Za-z' -]{0,119}[.!]?", replacement) and not declined(replacement):
                    state.name = normalize_name(replacement)
                    return self._name_reply(session)
                state.stage = "name"
                state.name = ""
                return self._assistant(session, "What's the correct name?")
            if declined(transcript):
                info["declined"].append("name")
                return self._next_reply(session)
            attempts = info.setdefault("confirmation_attempts", {})
            attempts["name"] = attempts.get("name", 0) + 1
            return self._unavailable_contact(session, "name") if attempts["name"] >= 2 else self._confirmation(session, "name")

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
                return await self._after_confirmation(session)
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
            return self._confirmation(session, "email", spell_email=spell_email)

        if state.stage == "phone":
            state.phone = phone_answer(transcript)
            if not state.phone:
                return self._retry_contact(session, "phone", "Could you say the callback number again, including the area code?")
            state.stage = "confirm_phone"
            return self._confirmation(session, "phone")

        if state.stage == "confirm_phone":
            decision = normalize_confirmation(transcript)
            if decision == "yes":
                state.confirmations["phone"] = state.phone
                return await self._after_confirmation(session)
            replacement = normalize_phone(transcript)
            if replacement:
                state.phone = replacement
                return self._confirmation(session, "phone")
            if decision == "no":
                state.stage = "phone"
                state.phone = ""
                return self._assistant(session, "What's the correct callback number?")
            attempts = info.setdefault("confirmation_attempts", {})
            attempts["phone"] = attempts.get("phone", 0) + 1
            return self._unavailable_contact(session, "phone") if attempts["phone"] >= 2 else self._confirmation(session, "phone")

        if state.stage == "address":
            candidate = address_answer(transcript)
            if not re.search(r"\d", candidate):
                return self._retry_contact(session, "address", "Could you give me the street address?")
            state.address = candidate
            area = self.business.service_area(state.address)
            state.service_area_status = area.status
            state.service_area_city = area.city
            state.stage = "confirm_address"
            return self._confirmation(session, "address")

        if state.stage == "confirm_address":
            decision = normalize_confirmation(transcript)
            replacement = address_answer(transcript)
            if re.match(r"\d", replacement) and replacement != state.address:
                state.address = clean(replacement, 500)
                state.confirmations.pop("address", None)
                area = self.business.service_area(state.address)
                state.service_area_status, state.service_area_city = area.status, area.city
                return self._confirmation(session, "address")
            if decision == "yes":
                state.confirmations["address"] = state.address
                return await self._after_confirmation(session)
            if decision == "no":
                state.metadata["rejected_address"] = state.address
                corrections = info.setdefault("correction_attempts", {})
                corrections["address"] = corrections.get("address", 0) + 1
                if corrections["address"] >= 2:
                    return self._unavailable_contact(session, "address")
                state.stage = "address"
                state.address = ""
                state.confirmations.pop("address", None)
                state.service_area_status, state.service_area_city = "unknown", ""
                return self._assistant(session, "Let's get that right. Please repeat the service address.")
            attempts = info.setdefault("confirmation_attempts", {})
            attempts["address"] = attempts.get("address", 0) + 1
            return self._unavailable_contact(session, "address") if attempts["address"] >= 2 else self._confirmation(session, "address")

        state.stage = "issue"
        return self._assistant(session, collection_question(state))

    async def _notify(self, session: CallSession, *, kind: str, partial: bool) -> None:
        self._save(session)
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
        if session.state.stage == "done":
            reply = self._planned(session, "Thanks for calling Floodman. Goodbye.")
            reply.end_call = True
            return reply
        info = memory(session.state)
        if session.introduction_pending:
            session.introduction_pending = False
            return self._planned(session, self.greeting())
        if info.get("speech_fragments") and session.no_input_count == 0:
            session.no_input_count = 1
            return self._planned(session, "Go ahead, I'm listening.")
        if info.get("hold_timeouts", 0) > 0:
            info["hold_timeouts"] -= 1
            self._save(session)
            return VoiceReply(text="", wait_for_caller=True)
        if info.get("wrapup_followup") and not info.get("speech_fragments") and session.state.stage in {"anything_else", "additional_message"}:
            return await self._finish_intake(session)
        session.no_input_count += 1
        if session.no_input_count == 1:
            return self._planned(session, "Are you still there? I can wait a moment.")
        if session.no_input_count == 2:
            return self._planned(session, "I'm still here. Say hello when you're ready.")
        await self._notify(session, kind="partial_no_input", partial=True)
        reply = self._assistant(session, "I'm still not hearing you, so I'll send the information I have to the team. Please call back when you're ready. Goodbye.")
        reply.end_call = True
        return reply

    async def disconnect(self, session: CallSession, outcome: str = "caller_hangup") -> None:
        await self.finalize_completion(session)
        if not session.state.completed and not session.notification_sent:
            await self._notify(session, kind="partial_hangup", partial=True)
        self.database.finish_call(session.call_id, outcome)
        if self.briefings is not None:
            try:
                self.briefings.queue(session.call_id)
            except Exception as exc:
                logger.warning("Unable to queue post-call briefing: %s", type(exc).__name__)
        if self.suite_bridge is not None:
            event_type = "call-failed" if outcome == "error" else "call-ended"
            failure_reason = "Voice processing failed; local partial intake retained" if event_type == "call-failed" else ""
            self.suite_bridge.queue(
                session.call_id,
                session.state,
                event_type,
                failure_reason=failure_reason,
            )
