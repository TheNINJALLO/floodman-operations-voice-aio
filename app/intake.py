from __future__ import annotations

import json
import re
from typing import Any, Mapping


PHONE_RE = re.compile(r"\+[1-9][0-9]{7,14}")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

INTAKE_TEXT_FIELDS = (
    "direction",
    "caller_number",
    "name",
    "phone",
    "email",
    "email_status",
    "address",
    "service_requested",
    "service_key",
    "service_status",
    "service_reason",
    "description",
    "property_context",
    "safety_summary",
    "timing_summary",
    "insurance_summary",
    "evidence_summary",
    "urgency",
    "department",
    "status",
    "summary",
    "outcome",
    "customer_id",
    "property_id",
    "lead_id",
    "callback_id",
    "notification_status",
    "completed_at",
)

SUPPORTED_ALIASES: dict[str, tuple[str, ...]] = {
    "water_damage_restoration": (
        "water damage",
        "water mitigation",
        "water restoration",
        "flood cleanup",
        "flooded basement",
        "water extraction",
        "structural drying",
        "burst pipe",
        "water heater leak",
        "active leak cleanup",
    ),
    "mold_remediation": (
        "mold",
        "mould",
        "mold remediation",
        "mold removal",
        "microbial growth",
        "musty odor",
    ),
    "foundation_repair": (
        "foundation repair",
        "foundation crack",
        "bowing wall",
        "bulging wall",
        "leaning wall",
        "settlement",
        "structural wall",
        "carbon fiber",
    ),
    "basement_waterproofing": (
        "basement waterproofing",
        "waterproofing",
        "wet basement",
        "basement seepage",
        "wall seepage",
        "floor seepage",
        "interior drainage",
        "drain tile",
        "moisture management",
    ),
    "crawl_space_encapsulation": (
        "crawl space",
        "crawlspace",
        "encapsulation",
        "vapor barrier",
        "crawl space moisture",
    ),
    "sump_pump_and_drainage": (
        "sump pump",
        "sump-pump",
        "drainage",
        "yard drainage",
        "foundation drainage",
        "discharge line",
        "pit overflowing",
        "pump alarm",
    ),
}

SERVICE_INTAKE_QUESTIONS: dict[str, tuple[str, ...]] = {
    "water_damage_restoration": (
        "When did the water loss begin, and is water still entering or rising?",
        "What is the known or suspected source, and has it been stopped?",
        "Which rooms, areas, and materials are wet or damaged?",
        "Is sewage, contaminated water, electricity, or a structural hazard involved?",
        "Is the property safe to enter, and are photos or video available?",
    ),
    "mold_remediation": (
        "Where is the visible growth or musty odor, and how large is the affected area?",
        "When was it first noticed, and has it returned after prior cleaning or repairs?",
        "Is there a known leak, condensation issue, flooding event, or other moisture source?",
        "Which rooms, materials, or hidden cavities may be affected?",
        "Has testing, remediation, or related repair work already been completed?",
    ),
    "foundation_repair": (
        "Where are the cracks, bowing, leaning, settlement, or movement visible?",
        "When was the condition first noticed, and is it changing or worsening?",
        "Is water entering through the affected wall, floor, or crack?",
        "Are doors, windows, floors, or other parts of the structure affected?",
        "Has any prior foundation repair, reinforcement, or engineering review been completed?",
    ),
    "basement_waterproofing": (
        "Where does water or moisture enter, and which basement areas are affected?",
        "Does it happen during rain, snowmelt, sump-pump activity, or another condition?",
        "Is there standing water, seepage, dampness, efflorescence, or a musty odor?",
        "What drainage, sump-pump, crack repair, or waterproofing work has been tried before?",
        "When did the problem begin, and is it recurring or getting worse?",
    ),
    "crawl_space_encapsulation": (
        "Is there standing water, wet soil, high humidity, condensation, or a musty odor?",
        "Are insulation, floor framing, vapor barriers, or stored materials affected?",
        "Is visible growth, pest activity, drainage trouble, or structural damage present?",
        "What moisture-control, drainage, insulation, or encapsulation work exists now?",
        "When was the condition first noticed, and is it seasonal or recurring?",
    ),
    "sump_pump_and_drainage": (
        "Is the pump running, alarming, overflowing, cycling constantly, or not operating?",
        "Is water actively entering or rising, and which areas are affected?",
        "Where does the discharge line run, and is flow blocked, frozen, or backing up?",
        "Does the problem occur during rain, snowmelt, or another repeatable condition?",
        "Has the pump, check valve, pit, drainage, or discharge system been serviced before?",
    ),
}


KNOWN_UNSUPPORTED: dict[str, tuple[str, ...]] = {
    "roofing": (
        "roof repair",
        "replace roof",
        "roofing",
        "shingle repair",
        "install shingles",
    ),
    "plumbing": (
        "plumber",
        "plumbing repair",
        "replace pipe",
        "fix faucet",
        "sewer line repair",
    ),
    "electrical": (
        "electrician",
        "electrical repair",
        "rewire",
        "breaker repair",
    ),
    "hvac": (
        "hvac",
        "furnace repair",
        "air conditioner",
        "ac repair",
        "duct cleaning",
    ),
    "septic": ("septic pumping", "septic repair", "septic tank"),
    "pest_control": (
        "pest control",
        "termite",
        "exterminator",
        "rodent removal",
    ),
    "general_remodeling": (
        "kitchen remodel",
        "bathroom remodel",
        "general remodeling",
        "painting only",
        "drywall only",
        "handyman",
    ),
    "landscaping": (
        "landscaping",
        "tree removal",
        "lawn care",
        "snow removal",
    ),
    "specialty_hazard": (
        "asbestos removal",
        "lead paint removal",
        "biohazard cleanup",
    ),
}


def clean_text(value: Any, *, maximum: int = 12000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:maximum]


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    return email[:320] if EMAIL_RE.fullmatch(email) else ""


def normalize_email_status(value: Any) -> str:
    raw = clean_text(value, maximum=40).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "provided": "provided",
        "available": "provided",
        "declined": "declined",
        "refused": "declined",
        "no_email": "unavailable",
        "none": "unavailable",
        "unavailable": "unavailable",
        "unknown": "unknown",
        "not_asked": "not_asked",
    }
    return aliases.get(raw, raw if raw in aliases.values() else "")


def normalize_service_status(value: Any) -> str:
    raw = clean_text(value, maximum=40).lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "yes": "supported",
        "offered": "supported",
        "supported": "supported",
        "no": "unsupported",
        "not_offered": "unsupported",
        "unsupported": "unsupported",
        "review": "review",
        "unconfirmed": "review",
        "unclear": "review",
        "unknown": "unknown",
    }
    return aliases.get(raw, raw if raw in aliases.values() else "")


def merge_intake_snapshot(
    existing: Mapping[str, Any] | None,
    update: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(existing or {})
    incoming = dict(update or {})
    for field in INTAKE_TEXT_FIELDS:
        if field not in incoming or incoming[field] is None:
            continue
        if field == "email":
            value = normalize_email(incoming[field])
        elif field == "email_status":
            value = normalize_email_status(incoming[field])
        elif field == "service_status":
            value = normalize_service_status(incoming[field])
        else:
            maximum = 50000 if field == "summary" else 12000
            value = clean_text(incoming[field], maximum=maximum)
        if value:
            merged[field] = value

    if merged.get("email"):
        merged["email_status"] = "provided"

    if "transcript" in incoming and incoming.get("transcript") is not None:
        merged["transcript"] = incoming.get("transcript")
    if "transcript_text" in incoming and incoming.get("transcript_text") is not None:
        merged["transcript_text"] = str(incoming.get("transcript_text") or "")[:100000]
    if "metadata" in incoming and isinstance(incoming.get("metadata"), Mapping):
        prior = dict(merged.get("metadata") or {})
        prior.update(dict(incoming["metadata"]))
        merged["metadata"] = prior
    if "notification_ids" in incoming and isinstance(incoming.get("notification_ids"), list):
        merged["notification_ids"] = list(incoming["notification_ids"])
    if "notification_count" in incoming and incoming.get("notification_count") is not None:
        try:
            merged["notification_count"] = max(0, int(incoming["notification_count"]))
        except (TypeError, ValueError):
            pass
    return merged


def intake_missing_fields(snapshot: Mapping[str, Any]) -> list[str]:
    missing: list[str] = []
    if not clean_text(snapshot.get("name")):
        missing.append("full name")
    phone = clean_text(snapshot.get("phone") or snapshot.get("caller_number"), maximum=40)
    if not PHONE_RE.fullmatch(phone):
        missing.append("callback number")
    if not clean_text(snapshot.get("address")):
        missing.append("full property address")
    email = normalize_email(snapshot.get("email"))
    email_status = normalize_email_status(snapshot.get("email_status"))
    if not email and email_status not in {"declined", "unavailable"}:
        missing.append("email address or confirmation that none is available")
    if not clean_text(snapshot.get("description")):
        missing.append("detailed description of what is happening")
    if normalize_service_status(snapshot.get("service_status")) not in {
        "supported",
        "unsupported",
        "review",
    }:
        missing.append("service review")
    return missing


def _normalized_words(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _phrase_score(haystack: str, phrases: tuple[str, ...]) -> int:
    """Score whole-word phrase matches without substring collisions.

    For example, ``roofing`` must not match ``waterproofing``.
    """
    score = 0
    for phrase in phrases:
        normalized = _normalized_words(phrase)
        if not normalized:
            continue
        pattern = rf"(?:^| ){re.escape(normalized)}(?:$| )"
        if re.search(pattern, haystack):
            score += max(2, len(normalized.split()))
    return score


def classify_service_request(
    service_information: Mapping[str, Any] | None,
    requested_service: Any,
    description: Any = "",
) -> dict[str, Any]:
    requested = clean_text(requested_service, maximum=300)
    details = clean_text(description, maximum=4000)
    requested_haystack = _normalized_words(requested)
    detail_haystack = _normalized_words(details)

    # Only use the explicitly requested work for a hard out-of-scope decision.
    # A caller may mention a broken pipe while asking for water mitigation, which
    # is different from asking Floodman to perform plumbing repairs.
    for category, phrases in KNOWN_UNSUPPORTED.items():
        if _phrase_score(requested_haystack, phrases) > 0:
            label = requested or category.replace("_", " ")
            return {
                "ok": True,
                "supported": False,
                "service_status": "unsupported",
                "service_key": "",
                "public_name": label,
                "service_reason": f"known_out_of_scope:{category}",
                "intake_questions": [],
                "no_phone_promises": [],
                "safe_message": (
                    f"Floodman does not currently offer {label}. "
                    "I can still collect the details and forward them to the team for review."
                ),
            }

    business = dict(service_information or {})
    services_raw = business.get("services") or {}
    services = services_raw if isinstance(services_raw, Mapping) else {}
    best_key = ""
    best_score = 0
    for key, raw in services.items():
        if not isinstance(raw, Mapping) or not raw.get("website_advertises", True):
            continue
        public_name = clean_text(raw.get("public_name") or key, maximum=300)
        aliases = list(SUPPORTED_ALIASES.get(str(key), ()))
        aliases.extend((str(key).replace("_", " "), public_name))
        phrase_tuple = tuple(value for value in aliases if value)
        score = _phrase_score(requested_haystack, phrase_tuple) * 4
        score += _phrase_score(detail_haystack, phrase_tuple)
        if score > best_score:
            best_key = str(key)
            best_score = score

    if best_key:
        raw = services.get(best_key) or {}
        public_name = clean_text(
            raw.get("public_name") or best_key.replace("_", " "),
            maximum=300,
        )
        configured_questions = raw.get("intake_questions", [])
        if not isinstance(configured_questions, (list, tuple)):
            configured_questions = []
        intake_questions = [
            clean_text(item, maximum=500)
            for item in configured_questions
            if clean_text(item)
        ]
        if not intake_questions:
            intake_questions = [
                clean_text(item, maximum=500)
                for item in SERVICE_INTAKE_QUESTIONS.get(best_key, ())
                if clean_text(item)
            ]
        return {
            "ok": True,
            "supported": True,
            "service_status": "supported",
            "service_key": best_key,
            "public_name": public_name,
            "service_reason": "matched_approved_service_catalog",
            "intake_questions": intake_questions,
            "no_phone_promises": [
                clean_text(item, maximum=500)
                for item in raw.get("no_phone_promises", [])
                if clean_text(item)
            ],
            "safe_message": (
                f"Floodman offers {public_name}. I will gather the details the team needs."
            ),
        }

    label = requested or "that service"
    return {
        "ok": True,
        "supported": None,
        "service_status": "review",
        "service_key": "",
        "public_name": label,
        "service_reason": "not_matched_to_approved_service_catalog",
        "intake_questions": [],
        "no_phone_promises": [],
        "safe_message": (
            f"I cannot confirm {label} from Floodman's approved service list. "
            "I will still collect the details and forward them to the team for review."
        ),
    }


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        return _content_text(
            value.get("text")
            or value.get("content")
            or value.get("transcript")
            or value.get("message")
            or ""
        )
    if isinstance(value, list):
        return " ".join(part for item in value if (part := _content_text(item)))
    return clean_text(value)


def flatten_transcript(value: Any) -> str:
    transcript = value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"[", "{"}:
            try:
                transcript = json.loads(stripped)
            except (json.JSONDecodeError, TypeError, ValueError):
                return stripped[:100000]
        else:
            return stripped[:100000]

    if isinstance(transcript, Mapping):
        for key in ("messages", "turns", "transcript", "items"):
            if isinstance(transcript.get(key), list):
                transcript = transcript[key]
                break
        else:
            return _content_text(transcript)[:100000]

    if not isinstance(transcript, list):
        return _content_text(transcript)[:100000]

    lines: list[str] = []
    for item in transcript:
        if isinstance(item, Mapping):
            role = clean_text(
                item.get("role") or item.get("speaker") or item.get("type") or "",
                maximum=60,
            ).lower()
            text = _content_text(
                item.get("content")
                or item.get("text")
                or item.get("transcript")
                or item.get("message")
                or ""
            )
        else:
            role = ""
            text = _content_text(item)
        if not text:
            continue
        if role in {"user", "caller", "customer", "human"}:
            label = "Caller"
        elif role in {"assistant", "agent", "ava", "ai"}:
            label = "Ava"
        elif role in {"system", "tool"}:
            continue
        else:
            label = role.title() if role else "Conversation"
        lines.append(f"{label}: {text}")
    return "\n".join(lines)[:100000]


def transcript_excerpt(value: Any, maximum: int = 650) -> str:
    text = flatten_transcript(value) if not isinstance(value, str) else value
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= maximum:
        return text
    return text[: max(0, maximum - 1)].rstrip() + "…"


def infer_partial_status(outcome: Any, summary: Any = "", metadata: Any = None) -> str:
    combined = " ".join(
        (
            clean_text(outcome, maximum=300),
            clean_text(summary, maximum=1000),
            clean_text(metadata, maximum=1000),
        )
    ).lower()
    if "no_input" in combined or "no input" in combined or "inactivity" in combined:
        return "partial_no_input"
    if any(
        term in combined
        for term in (
            "provider failure",
            "provider_error",
            "llm",
            "rate limit",
            "pipeline failure",
        )
    ):
        return "partial_provider_failure"
    if any(
        term in combined
        for term in (
            "hangup",
            "hung up",
            "caller disconnected",
            "caller_disconnect",
        )
    ):
        return "partial_hangup"
    return "partial_call_ended"


def has_meaningful_intake(snapshot: Mapping[str, Any]) -> bool:
    # Caller ID alone is useful for an inbound hangup because the team still
    # needs to know which number called, even when no speech was recovered.
    for key in (
        "caller_number",
        "phone",
        "name",
        "email",
        "address",
        "service_requested",
        "description",
        "property_context",
        "safety_summary",
        "timing_summary",
    ):
        if clean_text(snapshot.get(key)):
            return True
    transcript = str(snapshot.get("transcript_text") or "")
    caller_words = re.findall(
        r"(?:Caller|Customer|User):\s*([^\n]+)",
        transcript,
        flags=re.I,
    )
    return sum(len(value.split()) for value in caller_words) >= 2
