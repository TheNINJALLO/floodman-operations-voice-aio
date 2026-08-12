from __future__ import annotations

from datetime import datetime, timezone

from app.compliance.engine import ComplianceEngine
from app.db import Database
from app.models import ConsentSnapshot, EligibilityRequest, OutboundPurpose


def at_business_time() -> datetime:
    # Wednesday at 10:00 AM America/Detroit during daylight time.
    return datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)


def test_requested_callback_with_recent_request_is_allowed(settings):
    db = Database(settings.database_path)
    engine = ComplianceEngine(settings, db)
    decision = engine.evaluate(
        EligibilityRequest(
            phone="231-555-1212",
            purpose=OutboundPurpose.REQUESTED_CALLBACK,
            scheduled_for=at_business_time(),
            requested_at=datetime(2026, 8, 11, 18, 0, tzinfo=timezone.utc),
        )
    )
    assert decision.allowed is True
    assert decision.consent_type == "explicit_callback_request"
    db.close()


def test_marketing_requires_written_consent_and_external_attestations(settings):
    db = Database(settings.database_path)
    engine = ComplianceEngine(settings, db)
    missing = engine.evaluate(
        EligibilityRequest(
            phone="+12315551212",
            purpose=OutboundPurpose.WINBACK,
            scheduled_for=at_business_time(),
        )
    )
    assert missing.allowed is False
    assert missing.reason == "missing_or_revoked_consent"

    consent = ConsentSnapshot(
        phone="+12315551212",
        marketing_voice_written=True,
        source="signed-estimate-form",
        text_version="2026-08",
        consent_text="Floodman may place automated marketing voice calls to this number.",
        consented_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    dnc_missing = engine.evaluate(
        EligibilityRequest(
            phone="+12315551212",
            purpose=OutboundPurpose.WINBACK,
            scheduled_for=at_business_time(),
            consent=consent,
        )
    )
    assert dnc_missing.reason == "dnc_check_required"

    allowed = engine.evaluate(
        EligibilityRequest(
            phone="+12315551212",
            purpose=OutboundPurpose.WINBACK,
            scheduled_for=at_business_time(),
            consent=consent,
            metadata={
                "dnc_status": "clear",
                "dnc_checked_at": "2026-08-11T12:00:00+00:00",
                "reassignment_status": "not_reassigned",
                "reassignment_checked_at": "2026-08-11T12:00:00+00:00",
            },
        )
    )
    assert allowed.allowed is True
    db.close()


def test_suppression_overrides_consent(settings):
    db = Database(settings.database_path)
    db.suppress("+12315551212", "customer_request", ["all"], "test")
    engine = ComplianceEngine(settings, db)
    decision = engine.evaluate(
        EligibilityRequest(
            phone="+12315551212",
            purpose=OutboundPurpose.BILLING_REMINDER,
            scheduled_for=at_business_time(),
            consent=ConsentSnapshot(phone="+12315551212", transactional_voice=True),
        )
    )
    assert decision.allowed is False
    assert decision.reason == "suppressed"
    db.close()


def test_consent_evidence_is_required_and_future_dates_fail_closed(settings):
    db = Database(settings.database_path)
    engine = ComplianceEngine(settings, db)
    missing = engine.evaluate(
        EligibilityRequest(
            phone="+12315550010",
            purpose=OutboundPurpose.BILLING_REMINDER,
            scheduled_for=at_business_time(),
            consent=ConsentSnapshot(phone="+12315550010", transactional_voice=True),
        )
    )
    assert missing.reason == "transactional_consent_evidence_required"
    future = engine.evaluate(
        EligibilityRequest(
            phone="+12315550010",
            purpose=OutboundPurpose.BILLING_REMINDER,
            scheduled_for=at_business_time(),
            consent=ConsentSnapshot(
                phone="+12315550010",
                transactional_voice=True,
                source="agreement",
                text_version="v1",
                consent_text="Automated account calls are authorized.",
                consented_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
            ),
        )
    )
    assert future.reason == "consent_timestamp_invalid"
    db.close()


def test_partial_marketing_revocation_preserves_transactional_consent(settings):
    db = Database(settings.database_path)
    db.upsert_consent(
        {
            "phone": "+12315550011",
            "transactional_voice": True,
            "marketing_voice_written": True,
            "source": "agreement",
            "text_version": "v1",
            "consent_text": "Automated account and marketing calls are authorized.",
            "consented_at": "2026-08-01T12:00:00+00:00",
        }
    )
    revoked = db.revoke_consent("+12315550011", ["marketing"])
    assert revoked is not None
    assert revoked["marketing_voice_written"] is False
    assert revoked["transactional_voice"] is True
    assert revoked["revoked_at"] is None
    assert revoked["raw"]["revocations"][-1]["categories"] == ["marketing"]
    decision = ComplianceEngine(settings, db).evaluate(
        EligibilityRequest(
            phone="+12315550011",
            purpose=OutboundPurpose.BILLING_REMINDER,
            scheduled_for=at_business_time(),
        )
    )
    assert decision.allowed is True
    db.close()
