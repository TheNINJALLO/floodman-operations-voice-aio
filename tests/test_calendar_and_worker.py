from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.calendar import LocalAvailability
from app.compliance.engine import ComplianceEngine
from app.db import Database
from app.models import JobStatus, OutboundJobCreate, OutboundPurpose
from app.outbound.ami import AMIClient
from app.outbound.worker import OutboundWorker
from app.roomflow.client import RoomflowClient


def test_server_side_appointment_validation_blocks_off_hours_and_conflicts(settings):
    db = Database(settings.database_path)
    availability = LocalAvailability(settings, db)
    now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)  # Wednesday, 4 AM Detroit
    off_hours = availability.validate_slot(
        "2026-08-12T03:00:00-04:00",
        "2026-08-12T04:30:00-04:00",
        "America/Detroit",
        now=now,
    )
    assert off_hours["error"] == "appointment_lead_time_required" or off_hours["error"] == "appointment_outside_business_hours"

    valid = availability.validate_slot(
        "2026-08-12T10:00:00-04:00",
        "2026-08-12T11:30:00-04:00",
        "America/Detroit",
        now=now,
    )
    assert valid["ok"] is True
    customer = db.upsert_customer({"name": "Calendar Customer", "phone": "+12315550888"})
    db.create_appointment(
        {
            "customer_id": customer["id"],
            "start": valid["start"],
            "end": valid["end"],
            "timezone": "America/Detroit",
        }
    )
    conflict = availability.validate_slot(
        "2026-08-12T10:00:00-04:00",
        "2026-08-12T11:30:00-04:00",
        "America/Detroit",
        now=now,
    )
    assert conflict["error"] == "appointment_slot_conflict"
    db.close()


@pytest.mark.asyncio
async def test_stale_accepted_outbound_call_requires_manual_review_not_redial(settings):
    db = Database(settings.database_path)
    job = db.create_outbound_job(
        OutboundJobCreate(
            phone="+12315550777",
            purpose=OutboundPurpose.REQUESTED_CALLBACK,
            requested_at=datetime.now(timezone.utc),
        ),
        "floodman_callback",
    )
    db.update_job(job["id"], status=JobStatus.ANSWERED.value, attempts=1)
    stale = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    db.execute("UPDATE outbound_jobs SET updated_at=? WHERE id=?", (stale, job["id"]))
    worker = OutboundWorker(
        settings,
        db,
        ComplianceEngine(settings, db),
        AMIClient(settings),
        RoomflowClient(settings, db),
    )
    settings.dialing_timeout_seconds = 60
    await worker.recover_stale_jobs()
    updated = db.get_outbound_job(job["id"])
    assert updated is not None
    assert updated["status"] == JobStatus.FAILED.value
    assert updated["last_error"] == "completion_status_unknown_manual_review"
    assert updated["attempts"] == 1
    db.close()
