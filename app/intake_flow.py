from __future__ import annotations

import re
from typing import Any, Mapping

from app.intake import (
    PHONE_RE,
    clean_text,
    intake_missing_fields,
    normalize_email,
    normalize_email_status,
)


CONTACT_ORDER: tuple[str, ...] = (
    "name",
    "email",
    "phone",
    "address",
)

CONTACT_LABELS: dict[str, str] = {
    "name": "full name",
    "email": "email address",
    "phone": "callback number",
    "address": "property address",
}

CONTACT_DATA_MISSING = {
    "full name",
    "email address or confirmation that none is available",
    "callback number",
    "full property address",
}

AFFIRMATIVE = {
    "yes",
    "y",
    "yeah",
    "yep",
    "correct",
    "confirmed",
    "right",
    "that is right",
    "that's right",
    "sounds right",
}

NEGATIVE = {
    "no",
    "n",
    "nope",
    "incorrect",
    "wrong",
    "correction",
    "correct it",
    "change it",
    "not right",
    "that's wrong",
}


def _metadata(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    raw = snapshot.get("metadata")
    return dict(raw) if isinstance(raw, Mapping) else {}


def normalize_contact_field(value: Any) -> str:
    field = clean_text(value, maximum=40).lower().replace("-", "_")
    aliases = {
        "full_name": "name",
        "caller_name": "name",
        "email_address": "email",
        "callback": "phone",
        "callback_number": "phone",
        "phone_number": "phone",
        "property_address": "address",
    }
    field = aliases.get(field, field)
    return field if field in CONTACT_ORDER else ""


def normalize_confirmation(value: Any) -> str:
    raw = re.sub(
        r"[^a-z0-9']+",
        " ",
        str(value or "").strip().lower(),
    ).strip()
    if raw in AFFIRMATIVE:
        return "yes"
    if raw in NEGATIVE:
        return "no"
    if raw.startswith("yes ") or raw.startswith("correct "):
        return "yes"
    if raw.startswith("no ") or raw.startswith("wrong "):
        return "no"
    return ""


def contact_value(snapshot: Mapping[str, Any], field: str) -> str:
    field = normalize_contact_field(field)
    if field == "name":
        return clean_text(snapshot.get("name"), maximum=200)
    if field == "email":
        email = normalize_email(snapshot.get("email"))
        if email:
            return email
        status = normalize_email_status(snapshot.get("email_status"))
        if status in {"declined", "unavailable"}:
            return f"status:{status}"
        return ""
    if field == "phone":
        phone = clean_text(
            snapshot.get("phone") or snapshot.get("caller_number"),
            maximum=40,
        )
        return phone if PHONE_RE.fullmatch(phone) else ""
    if field == "address":
        return clean_text(snapshot.get("address"), maximum=500)
    return ""


def contact_confirmations(
    snapshot: Mapping[str, Any],
) -> dict[str, str]:
    values = _metadata(snapshot).get("contact_confirmations")
    return (
        {
            str(key): str(value)
            for key, value in dict(values).items()
        }
        if isinstance(values, Mapping)
        else {}
    )


def contact_rejections(
    snapshot: Mapping[str, Any],
) -> dict[str, str]:
    values = _metadata(snapshot).get("contact_rejections")
    return (
        {
            str(key): str(value)
            for key, value in dict(values).items()
        }
        if isinstance(values, Mapping)
        else {}
    )


def contact_is_confirmed(
    snapshot: Mapping[str, Any],
    field: str,
) -> bool:
    current = contact_value(snapshot, field)
    return bool(
        current
        and contact_confirmations(snapshot).get(field) == current
    )


def update_confirmation_metadata(
    existing: Mapping[str, Any],
    merged: Mapping[str, Any],
    *,
    confirm_field: Any = "",
    confirmation: Any = "",
) -> dict[str, Any]:
    """Apply one yes/no confirmation without losing volunteered fields."""

    metadata = _metadata(merged)
    metadata["contact_flow_enabled"] = True

    confirmations = contact_confirmations(merged)
    rejections = contact_rejections(merged)

    # Any changed value automatically loses its old confirmation.
    for field in CONTACT_ORDER:
        current = contact_value(merged, field)
        if confirmations.get(field) != current:
            confirmations.pop(field, None)
        rejected = rejections.get(field)
        if rejected and rejected != current:
            rejections.pop(field, None)

    target = normalize_contact_field(confirm_field)
    decision = normalize_confirmation(confirmation)
    if target and decision:
        old_value = contact_value(existing, target)
        current_value = contact_value(merged, target)

        if decision == "yes" and current_value:
            confirmations[target] = current_value
            rejections.pop(target, None)
        elif decision == "no":
            confirmations.pop(target, None)
            rejected_value = old_value or current_value
            if rejected_value:
                rejections[target] = rejected_value

            # A correction supplied in the same utterance should be retained,
            # then confirmed on the next turn rather than asked for again.
            if (
                current_value
                and rejected_value
                and current_value != rejected_value
            ):
                rejections.pop(target, None)

    metadata["contact_confirmations"] = confirmations
    metadata["contact_rejections"] = rejections
    return metadata


def _spoken_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        groups = (digits[:3], digits[3:6], digits[6:])
        return ", ".join(" ".join(group) for group in groups)
    return " ".join(digits)


def _spoken_email(value: str) -> str:
    text = value.replace("_", " underscore ")
    text = text.replace("-", " dash ")
    text = text.replace("@", " at ")
    text = text.replace(".", " dot ")
    return re.sub(r"\s+", " ", text).strip()


def contact_collection_question(
    field: str,
    *,
    correction: bool = False,
) -> str:
    if field == "name":
        return (
            "What's the correct name?"
            if correction
            else "What name should I put this under?"
        )
    if field == "email":
        return (
            "What's the correct email?"
            if correction
            else "What's the best email for you? You can say skip."
        )
    if field == "phone":
        return (
            "What's the correct callback number?"
            if correction
            else "What's the best callback number?"
        )
    if field == "address":
        return (
            "What's the correct service address?"
            if correction
            else "What's the full service address?"
        )
    return ""


def contact_confirmation_question(
    snapshot: Mapping[str, Any],
    field: str,
) -> str:
    value = contact_value(snapshot, field)
    if field == "name":
        return f"{value}, right?"
    if field == "email":
        if value.startswith("status:"):
            return "No email, right?"
        return f"{_spoken_email(value)}, right?"
    if field == "phone":
        caller = clean_text(
            snapshot.get("caller_number"),
            maximum=40,
        )
        if (
            caller
            and re.sub(r"\D", "", caller)
            == re.sub(r"\D", "", value)
        ):
            return (
                "Is this the best number to call you back on?"
            )
        return f"{_spoken_phone(value)}, right?"
    if field == "address":
        return f"{value}, right?"
    return ""


def _service_questions(
    snapshot: Mapping[str, Any],
    supplied: Any = (),
) -> tuple[str, ...]:
    values = supplied
    if not values:
        values = _metadata(snapshot).get("service_questions") or ()
    if isinstance(values, str):
        values = (values,)
    return tuple(
        clean_text(value, maximum=500)
        for value in values or ()
        if clean_text(value, maximum=500)
    )


def _single_detail_question(
    snapshot: Mapping[str, Any],
    missing: list[str],
    *,
    service_questions: Any = (),
) -> tuple[str, str]:
    missing_set = set(missing)

    if "detailed description of what is happening" in missing_set:
        return ("description", "What happened, and where?")

    if "service review" in missing_set:
        return (
            "service_requested",
            "What do you need help with?",
        )

    if "property type and caller relationship" in missing_set:
        return (
            "property_context",
            "Is this a home or a business?",
        )

    if (
        "when the issue began and whether it is active"
        in missing_set
    ):
        return ("timing_summary", "When did this start?")

    if "safety and access concerns" in missing_set:
        return (
            "safety_summary",
            "Any electrical, sewage, or other safety concerns?",
        )

    return ("", "")


def intake_submission_missing_fields(
    snapshot: Mapping[str, Any],
) -> list[str]:
    """Return detail gaps plus one-by-one contact confirmation gaps."""

    base = intake_missing_fields(snapshot)
    missing = [
        value
        for value in base
        if value not in CONTACT_DATA_MISSING
    ]

    for field in CONTACT_ORDER:
        label = CONTACT_LABELS[field]
        current = contact_value(snapshot, field)
        if not current:
            if field == "email":
                missing.append(
                    "email address or confirmation that none is available"
                )
            elif field == "address":
                missing.append("full property address")
            elif field == "phone":
                missing.append("callback number")
            else:
                missing.append("full name")
        elif not contact_is_confirmed(snapshot, field):
            missing.append(f"confirmed {label}")

    return missing


def next_intake_state(
    snapshot: Mapping[str, Any],
    *,
    service_questions: Any = (),
) -> dict[str, Any]:
    """Return exactly one next collection or confirmation action."""

    base_missing = intake_missing_fields(snapshot)
    detail_missing = [
        value
        for value in base_missing
        if value not in CONTACT_DATA_MISSING
    ]
    field, question = _single_detail_question(
        snapshot,
        detail_missing,
        service_questions=service_questions,
    )
    if question:
        return {
            "stage": "collect_detail",
            "field": field,
            "confirmation_required": False,
            "next_question": question,
            "safe_message": question,
            "missing": intake_submission_missing_fields(snapshot),
            "ready_to_submit": False,
        }

    rejections = contact_rejections(snapshot)
    for contact_field in CONTACT_ORDER:
        current = contact_value(snapshot, contact_field)
        rejected_current = bool(
            current
            and rejections.get(contact_field) == current
        )
        if not current or rejected_current:
            question = contact_collection_question(
                contact_field,
                correction=rejected_current,
            )
            return {
                "stage": "collect_contact",
                "field": contact_field,
                "confirmation_required": False,
                "next_question": question,
                "safe_message": question,
                "missing": intake_submission_missing_fields(snapshot),
                "ready_to_submit": False,
            }

        if not contact_is_confirmed(snapshot, contact_field):
            question = contact_confirmation_question(
                snapshot,
                contact_field,
            )
            return {
                "stage": "confirm_contact",
                "field": contact_field,
                "confirmation_required": True,
                "next_question": question,
                "safe_message": question,
                "missing": intake_submission_missing_fields(snapshot),
                "ready_to_submit": False,
            }

    return {
        "stage": "ready",
        "field": "",
        "confirmation_required": False,
        "next_question": "",
        "safe_message": "",
        "missing": intake_submission_missing_fields(snapshot),
        "ready_to_submit": True,
    }
