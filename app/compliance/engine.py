from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings
from app.db import Database
from app.models import ConsentSnapshot, EligibilityDecision, EligibilityRequest, OutboundPurpose

TRANSACTIONAL_PURPOSES = {
    OutboundPurpose.BILLING_REMINDER,
    OutboundPurpose.PAYMENT_FOLLOWUP,
}

EXPLICIT_CALLBACK_PURPOSES = {
    OutboundPurpose.REQUESTED_CALLBACK,
    OutboundPurpose.MISSED_CALL_CALLBACK,
}

MARKETING_PURPOSES = {
    OutboundPurpose.ESTIMATE_FOLLOWUP,
    OutboundPurpose.CANCELED_INSPECTION,
    OutboundPurpose.WINBACK,
    OutboundPurpose.MAINTENANCE,
}

_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


def normalize_phone(value: str) -> str:
    value = "".join(ch for ch in value if ch.isdigit() or ch == "+")
    if value.startswith("00"):
        value = "+" + value[2:]
    if not value.startswith("+") and len(value) == 10:
        value = "+1" + value
    return value


def _parse_clock(value: str, fallback: time) -> time:
    try:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))
    except (AttributeError, TypeError, ValueError):
        return fallback


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(slots=True)
class ComplianceEngine:
    settings: Settings
    database: Database

    def evaluate(self, request: EligibilityRequest) -> EligibilityDecision:
        phone = normalize_phone(request.phone)
        if not _PHONE_RE.match(phone):
            return EligibilityDecision(allowed=False, reason="invalid_phone")

        suppression = self.database.get_suppression(phone)
        if suppression and self._suppression_applies(suppression, request.purpose):
            return EligibilityDecision(
                allowed=False,
                reason="suppressed",
                policy={"suppression": suppression},
            )

        consent = request.consent or self._cached_consent(phone)
        consent_decision = self._check_consent(request, consent)
        if not consent_decision.allowed:
            return consent_decision

        external_checks = self._check_external_attestations(request)
        if not external_checks.allowed:
            external_checks.consent_type = consent_decision.consent_type
            return external_checks

        local_dt, tz = self._local_datetime(request.scheduled_for, request.timezone)
        if self._is_blackout_date(local_dt):
            retry = self._next_calling_window(local_dt + timedelta(days=1), tz, request.purpose)
            return EligibilityDecision(
                allowed=False,
                reason="blackout_date",
                retry_at=retry,
                consent_type=consent_decision.consent_type,
                policy={"local_date": local_dt.date().isoformat()},
            )

        emergency_callback = (
            request.purpose in EXPLICIT_CALLBACK_PURPOSES
            and str(request.metadata.get("urgency") or "").lower() == "emergency"
            and bool(self.settings.compliance_config.get("allow_emergency_callbacks_anytime", True))
        )
        if not emergency_callback:
            allowed_now, retry_at = self._within_calling_window(local_dt, tz, request.purpose)
            if not allowed_now:
                return EligibilityDecision(
                    allowed=False,
                    reason="outside_calling_window",
                    retry_at=retry_at,
                    consent_type=consent_decision.consent_type,
                    policy={"timezone": str(tz), "local_time": local_dt.isoformat()},
                )

        frequency = self._check_frequency(phone, request.purpose, local_dt.astimezone(timezone.utc))
        if not frequency.allowed:
            frequency.consent_type = consent_decision.consent_type
            return frequency

        return EligibilityDecision(
            allowed=True,
            reason="eligible",
            consent_type=consent_decision.consent_type,
            policy={
                "timezone": str(tz),
                "local_time": local_dt.isoformat(),
                "purpose": request.purpose.value,
                "emergency_window_override": emergency_callback,
                **external_checks.policy,
            },
        )

    def _cached_consent(self, phone: str) -> ConsentSnapshot | None:
        row = self.database.get_consent(phone)
        if not row:
            return None
        return ConsentSnapshot(
            customer_id=row.get("customer_id") or "",
            phone=phone,
            transactional_voice=bool(row.get("transactional_voice")),
            marketing_voice_written=bool(row.get("marketing_voice_written")),
            sms=bool(row.get("sms")),
            email=bool(row.get("email")),
            source=row.get("source") or "",
            text_version=row.get("text_version") or "",
            consent_text=row.get("consent_text") or "",
            consented_at=_parse_datetime(row.get("consented_at")),
            revoked_at=_parse_datetime(row.get("revoked_at")),
            raw=row.get("raw") or {},
        )

    def _check_consent(
        self, request: EligibilityRequest, consent: ConsentSnapshot | None
    ) -> EligibilityDecision:
        scheduled = _as_utc(request.scheduled_for)
        if request.purpose in EXPLICIT_CALLBACK_PURPOSES:
            validity_days = int(
                self.settings.compliance_config.get("callback_request_valid_days", 30)
            )
            if request.requested_at:
                requested = _as_utc(request.requested_at)
                age = scheduled - requested
                if timedelta(0) <= age <= timedelta(days=validity_days):
                    return EligibilityDecision(
                        allowed=True,
                        reason="explicit_callback_request",
                        consent_type="explicit_callback_request",
                    )
            if consent and consent.revoked_at is None and consent.transactional_voice:
                evidence = self._check_consent_evidence(consent, scheduled, "transactional")
                if not evidence.allowed:
                    return evidence
                return EligibilityDecision(
                    allowed=True,
                    reason="transactional_voice_consent",
                    consent_type="transactional_voice",
                )
            return EligibilityDecision(allowed=False, reason="missing_callback_request_or_consent")

        if not consent or consent.revoked_at is not None:
            return EligibilityDecision(allowed=False, reason="missing_or_revoked_consent")
        if normalize_phone(consent.phone) != normalize_phone(request.phone):
            return EligibilityDecision(allowed=False, reason="consent_phone_mismatch")
        if consent.customer_id and request.customer_id and consent.customer_id != request.customer_id:
            return EligibilityDecision(allowed=False, reason="consent_customer_mismatch")

        if request.purpose in TRANSACTIONAL_PURPOSES:
            if consent.transactional_voice:
                evidence = self._check_consent_evidence(consent, scheduled, "transactional")
                if not evidence.allowed:
                    return evidence
                return EligibilityDecision(
                    allowed=True,
                    reason="transactional_voice_consent",
                    consent_type="transactional_voice",
                )
            return EligibilityDecision(allowed=False, reason="transactional_voice_consent_required")

        if request.purpose in MARKETING_PURPOSES:
            if consent.marketing_voice_written:
                evidence = self._check_consent_evidence(consent, scheduled, "marketing")
                if not evidence.allowed:
                    return evidence
                return EligibilityDecision(
                    allowed=True,
                    reason="marketing_voice_written_consent",
                    consent_type="marketing_voice_written",
                )
            return EligibilityDecision(allowed=False, reason="marketing_voice_written_consent_required")

        return EligibilityDecision(allowed=False, reason="unsupported_purpose")

    def _check_consent_evidence(
        self, consent: ConsentSnapshot, scheduled: datetime, category: str
    ) -> EligibilityDecision:
        config = self.settings.compliance_config
        required_key = (
            "require_marketing_consent_evidence"
            if category == "marketing"
            else "require_transactional_consent_evidence"
        )
        if not bool(config.get(required_key, True)):
            return EligibilityDecision(allowed=True, reason="consent_evidence_not_required")
        missing = [
            key
            for key, value in (
                ("source", consent.source),
                ("text_version", consent.text_version),
                ("consent_text", consent.consent_text),
                ("consented_at", consent.consented_at),
            )
            if not value
        ]
        if missing:
            return EligibilityDecision(
                allowed=False,
                reason=f"{category}_consent_evidence_required",
                policy={"missing": missing},
            )
        consented_at = _as_utc(consent.consented_at)  # type: ignore[arg-type]
        if consented_at > scheduled + timedelta(minutes=5):
            return EligibilityDecision(
                allowed=False,
                reason="consent_timestamp_invalid",
                policy={"consented_at": consented_at.isoformat()},
            )
        return EligibilityDecision(allowed=True, reason="consent_evidence_valid")

    def _check_external_attestations(self, request: EligibilityRequest) -> EligibilityDecision:
        if request.purpose not in MARKETING_PURPOSES:
            return EligibilityDecision(allowed=True, reason="external_checks_not_required")
        config = self.settings.compliance_config
        metadata = request.metadata
        policy: dict[str, object] = {}

        if bool(config.get("require_dnc_check", True)):
            status = str(metadata.get("dnc_status") or "").lower()
            checked_at = _parse_datetime(metadata.get("dnc_checked_at"))
            max_age_days = int(config.get("dnc_max_age_days", 31))
            if status not in {"clear", "exempt"} or not checked_at:
                return EligibilityDecision(allowed=False, reason="dnc_check_required")
            age = _as_utc(request.scheduled_for) - checked_at.astimezone(timezone.utc)
            if age < timedelta(0) or age > timedelta(days=max_age_days):
                return EligibilityDecision(allowed=False, reason="dnc_check_expired")
            policy["dnc_status"] = status
            policy["dnc_checked_at"] = checked_at.isoformat()

        if bool(config.get("require_reassignment_check", True)):
            status = str(metadata.get("reassignment_status") or "").lower()
            checked_at = _parse_datetime(metadata.get("reassignment_checked_at"))
            max_age_days = int(config.get("reassignment_max_age_days", 30))
            if status not in {"clear", "not_reassigned", "safe_harbor"} or not checked_at:
                return EligibilityDecision(allowed=False, reason="reassignment_check_required")
            age = _as_utc(request.scheduled_for) - checked_at.astimezone(timezone.utc)
            if age < timedelta(0) or age > timedelta(days=max_age_days):
                return EligibilityDecision(allowed=False, reason="reassignment_check_expired")
            policy["reassignment_status"] = status
            policy["reassignment_checked_at"] = checked_at.isoformat()

        return EligibilityDecision(allowed=True, reason="external_checks_passed", policy=policy)

    def _suppression_applies(self, suppression: dict, purpose: OutboundPurpose) -> bool:
        categories = {str(value) for value in suppression.get("categories", [])}
        if "all" in categories:
            return True
        if purpose in MARKETING_PURPOSES and "marketing" in categories:
            return True
        if purpose in TRANSACTIONAL_PURPOSES and "transactional" in categories:
            return True
        if purpose in EXPLICIT_CALLBACK_PURPOSES and "callbacks" in categories:
            return True
        return purpose.value in categories

    def _local_datetime(self, value: datetime, timezone_name: str) -> tuple[datetime, ZoneInfo]:
        try:
            tz = ZoneInfo(timezone_name or self.settings.timezone)
        except ZoneInfoNotFoundError:
            try:
                tz = ZoneInfo(self.settings.timezone)
            except ZoneInfoNotFoundError:
                tz = ZoneInfo("UTC")
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(tz), tz

    def _is_blackout_date(self, local_dt: datetime) -> bool:
        values = self.settings.compliance_config.get("blackout_dates", [])
        return local_dt.date().isoformat() in {str(value) for value in values}

    def _window_for_day(
        self, weekday: int, purpose: OutboundPurpose
    ) -> tuple[time, time] | None:
        config = self.settings.compliance_config
        key = "marketing_hours" if purpose in MARKETING_PURPOSES else "transactional_hours"
        hours = config.get(key, {}) if isinstance(config.get(key, {}), dict) else {}
        day_name = [
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ][weekday]
        day = hours.get(day_name)
        if day in (None, False, "disabled"):
            return None
        if not isinstance(day, dict):
            day = {}
        default_start = time(9, 0)
        default_end = time(19, 30) if purpose not in MARKETING_PURPOSES else time(19, 0)
        return (
            _parse_clock(str(day.get("start", default_start.strftime("%H:%M"))), default_start),
            _parse_clock(str(day.get("end", default_end.strftime("%H:%M"))), default_end),
        )

    def _within_calling_window(
        self, local_dt: datetime, tz: ZoneInfo, purpose: OutboundPurpose
    ) -> tuple[bool, datetime | None]:
        window = self._window_for_day(local_dt.weekday(), purpose)
        if window:
            start, end = window
            current = local_dt.timetz().replace(tzinfo=None)
            if start <= current <= end:
                return True, None
        return False, self._next_calling_window(local_dt, tz, purpose)

    def _next_calling_window(
        self, local_dt: datetime, tz: ZoneInfo, purpose: OutboundPurpose
    ) -> datetime | None:
        blackout = {str(value) for value in self.settings.compliance_config.get("blackout_dates", [])}
        for offset in range(0, 15):
            candidate_date = (local_dt + timedelta(days=offset)).date()
            if candidate_date.isoformat() in blackout:
                continue
            candidate_window = self._window_for_day(candidate_date.weekday(), purpose)
            if not candidate_window:
                continue
            start, _ = candidate_window
            candidate = datetime.combine(candidate_date, start, tzinfo=tz)
            if candidate > local_dt:
                return candidate.astimezone(timezone.utc)
        return None

    def _check_frequency(
        self, phone: str, purpose: OutboundPurpose, now_utc: datetime
    ) -> EligibilityDecision:
        config = self.settings.compliance_config
        caps = (
            config.get("frequency_caps", {})
            if isinstance(config.get("frequency_caps", {}), dict)
            else {}
        )
        purpose_caps = (
            caps.get(purpose.value, {})
            if isinstance(caps.get(purpose.value, {}), dict)
            else {}
        )
        days = int(purpose_caps.get("window_days", 30))
        max_attempts = int(purpose_caps.get("max_attempts", 4))
        attempts = self.database.count_recent_attempts(
            phone, purpose.value, now_utc - timedelta(days=days)
        )
        if attempts >= max_attempts:
            return EligibilityDecision(
                allowed=False,
                reason="frequency_cap",
                retry_at=now_utc + timedelta(days=days),
                policy={"attempts": attempts, "max_attempts": max_attempts, "window_days": days},
            )

        cooldown_days = int(purpose_caps.get("after_live_contact_days", 7))
        last_contact = self.database.last_live_contact(phone)
        if last_contact and now_utc - last_contact < timedelta(days=cooldown_days):
            return EligibilityDecision(
                allowed=False,
                reason="recent_live_contact",
                retry_at=last_contact + timedelta(days=cooldown_days),
                policy={"last_contact": last_contact.isoformat()},
            )
        return EligibilityDecision(allowed=True, reason="frequency_ok")
