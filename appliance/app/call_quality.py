"""Evidence-based review of saved calls. Never changes prompts or business rules."""
from __future__ import annotations

import math
from collections import Counter
from typing import Any

from app.call_diagnostics import FEEDBACK_CATEGORIES
from app.db import Database, utcnow

REVIEW_VERSION = "call-quality-v1"
PATTERNS = {
    "slow_reply": ("Long reply waits", "Compare recognition, wording, and speech timings before changing the endpoint or model budget."),
    "recognition_failure": ("Recognition failed", "Review the recognition error and test recovery with a short caller reply and spelled contact details."),
    "audio_failure": ("Call or speech errors", "Check the call timeline and reproduce the failure before changing conversation wording."),
    "speech_fallback": ("Backup voice used", "Check local speech availability and listen to a controlled test call."),
    "input_audio_loss": ("Incoming audio dropped", "Check CPU contention and audio queue pressure with a controlled call."),
    "repeated_no_input": ("Repeated silence checks", "Review whether the caller was pausing, finished speaking, or unable to hear the assistant."),
    "wording_fallback": ("Approved wording used after model fallback", "Check the wording budget and CPU timings. An approved fallback alone does not mean speech failed."),
    "delivery_failure": ("Failed staff delivery attempts", "Check the saved delivery attempt and provider status before retrying a notification."),
}
for category, label in FEEDBACK_CATEGORIES.items():
    PATTERNS["reported_" + category] = ("Staff report: " + label.lower(), "Review the staff report against the call timeline and transcript, then add a regression case for a confirmed problem.")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _percentile(values: list[float], fraction: float) -> float | None:
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def review_call(call: dict[str, Any], diagnostics: dict[str, Any], slow_response_ms: float) -> dict[str, Any]:
    """Only operational evidence is returned; free text is never a policy input."""
    events = diagnostics["events"]
    counts = Counter(event["event"] for event in events)
    delays = [number for event in events if event["event"] == "first_audio" and event["turn"] > 0
              if (number := _number(event["details"].get("first_audio_ms"))) is not None]
    issues: dict[str, dict[str, Any]] = {}

    def add(key: str, matching: list[dict[str, Any]], count: int | None = None) -> None:
        if matching or count:
            issues[key] = {"code": key, "occurrences": count if count is not None else len(matching),
                           "event_ids": [event["id"] for event in matching[:12]]}

    add("slow_reply", [event for event in events if event["event"] == "first_audio" and event["turn"] > 0
                       and (delay := _number(event["details"].get("first_audio_ms"))) is not None
                       and delay > slow_response_ms])
    for key, names in {
        "recognition_failure": {"stt_failed"}, "audio_failure": {"tts_failed", "call_failed", "audio_error", "no_output_audio"},
        "speech_fallback": {"tts_fallback"}, "input_audio_loss": {"input_audio_dropped"},
        "wording_fallback": {"wording_fallback"},
    }.items():
        add(key, [event for event in events if event["event"] in names])
    if counts["no_input"] >= 2:
        add("repeated_no_input", [event for event in events if event["event"] == "no_input"])
    add("delivery_failure", [], int(call.get("failed_delivery_count", 0)))
    for category, count in Counter(item["category"] for item in diagnostics["feedback"]
                                   if item["category"] in FEEDBACK_CATEGORIES).items():
        add("reported_" + category, [], count)
    fingerprint = str(diagnostics["runtime"].get("code_fingerprint") or "")
    # A version is an opaque identifier, never arbitrary provider/customer text.
    if not fingerprint or len(fingerprint) > 64 or any(char not in "0123456789abcdef" for char in fingerprint):
        fingerprint = "unavailable"
    return {
        "call_id": call["id"], "started_at": call["started_at"],
        "code_fingerprint": fingerprint, "issues": list(issues.values()),
        "metrics": {"measured_replies": len(delays), "p50_first_audio_ms": _percentile(delays, .5),
                    "p95_first_audio_ms": _percentile(delays, .95), "max_first_audio_ms": max(delays) if delays else None,
                    "barge_ins": counts["barge_in"], "no_input_checks": counts["no_input"]},
        "coverage": {"event_count": diagnostics["event_count"], "events_truncated": diagnostics["events_truncated"],
                     "audio_timeline_available": bool(diagnostics["runtime"]),
                     "call_end_saved": bool(counts["call_ended"]),
                     "reported_dropped_events": sum(int(_number(event["details"].get("dropped_events")) or 0) for event in events)},
    }


def quality_report(database: Database, slow_response_ms: float, limit: int = 200) -> dict[str, Any]:
    calls = database.list_quality_calls(limit)
    reviews = [review_call(call, database.get_call_diagnostics(call["id"]), slow_response_ms) for call in calls]
    patterns: dict[str, dict[str, Any]] = {}
    for review in reviews:
        for issue in review["issues"]:
            key = issue["code"]
            if key not in patterns:
                title, next_step = PATTERNS[key]
                patterns[key] = {"code": key, "title": title, "next_step": next_step,
                                 "call_ids": [], "occurrences": 0}
            patterns[key]["call_ids"].append(review["call_id"])
            patterns[key]["occurrences"] += issue["occurrences"]
    return {
        "schema_version": 1, "review_version": REVIEW_VERSION, "generated_at": utcnow(),
        "sample_limit": max(1, min(limit, 200)), "calls_reviewed": len(reviews),
        "calls_with_observations": sum(bool(review["issues"]) for review in reviews),
        "calls_with_audio_timeline": sum(review["coverage"]["audio_timeline_available"] for review in reviews),
        "slow_response_ms": slow_response_ms,
        "patterns": sorted(patterns.values(), key=lambda row: (-len(row["call_ids"]), row["code"])),
        "calls": reviews,
        "coverage": {
            "sample": "Most recent ended calls; simulations and deployment checks excluded.",
            "timing": "Completed caller capture to first local PCM write. Excludes endpoint silence and carrier playback buffering.",
            "limitations": "Missing or truncated events are incomplete evidence. Barge-ins alone do not prove the assistant cut someone off. No audio recordings are analyzed.",
        },
        "improvement_process": ["Review an observation against its saved call evidence.",
                                "Add a deidentified regression case and prepare a versioned change.",
                                "Pass routing and conversation checks, then use controlled calls to verify the change.",
                                "Deploy a reviewed change and compare calls from the same code version; retain rollback."],
        "automatic_prompt_changes": False, "automatic_model_training": False,
    }
