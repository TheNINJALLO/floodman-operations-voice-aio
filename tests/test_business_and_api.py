from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.business import BusinessOperations
from app.db import Database
from app.roomflow.client import RoomflowClient


async def test_local_first_lead_queues_roomflow_write(settings):
    db = Database(settings.database_path)
    service = BusinessOperations(settings, db, RoomflowClient(settings, db))
    result = await service.execute(
        "create_lead",
        {
            "name": "Test Caller",
            "phone": "+12315550123",
            "address": "101 Water Street",
            "city": "Traverse City",
            "zip": "49684",
            "service": "basement waterproofing",
            "problem": "Water along the wall after rain",
            "urgency": "normal",
        },
        call_id="call-lead-1",
    )
    assert result["ok"] is True
    assert result["lead"]["id"]
    assert result["roomflow"]["queued"] is True
    assert len(db.list_customers()) == 1
    assert len(db.list_outbox()) == 1
    db.close()


def test_api_auth_crm_verification_and_upload(settings):
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/dashboard").status_code == 401
        headers = {"Authorization": "Bearer admin-test-token"}
        internal = {"X-Internal-Token": "internal-test-token"}

        customer = client.post(
            "/api/v1/crm/customers",
            headers=headers,
            json={"name": "Jamie Customer", "phone": "+12315551234", "email": "j@example.com"},
        ).json()
        customer_id = customer["id"]
        prop = client.post(
            "/api/v1/crm/properties",
            headers=headers,
            json={
                "customer_id": customer_id,
                "address": "455 River Road",
                "city": "Traverse City",
                "state": "MI",
                "zip": "49684",
            },
        )
        assert prop.status_code == 200
        invoice = client.post(
            "/api/v1/crm/invoices",
            headers=headers,
            json={
                "customer_id": customer_id,
                "invoice_number": "FM-1001",
                "amount_due": 250.0,
                "status": "open",
                "payment_url": "https://pay.example.test/FM-1001",
                "due_date": datetime(2026, 8, 20, tzinfo=timezone.utc).isoformat(),
            },
        ).json()

        protected_payload = {
            "call_id": "call-billing-1",
            "customer_id": customer_id,
            "caller_number": "+12315551234",
            "data": {"customer_id": customer_id, "invoice_id": invoice["id"]},
        }
        blocked = client.post(
            "/internal/tools/get-billing-summary", headers=internal, json=protected_payload
        ).json()
        assert blocked["error"] == "identity_verification_required"

        verified = client.post(
            "/internal/tools/verify-customer",
            headers=internal,
            json={
                "call_id": "call-billing-1",
                "customer_id": customer_id,
                "caller_number": "+12315551234",
                "data": {
                    "customer_id": customer_id,
                    "phone": "+12315551234",
                    "name": "Jamie Customer",
                    "street_number": "455",
                    "zip": "49684",
                },
            },
        ).json()
        assert verified["verified"] is True
        billing = client.post(
            "/internal/tools/get-billing-summary", headers=internal, json=protected_payload
        ).json()
        assert billing["ok"] is True
        assert billing["invoices"][0]["invoice_number"] == "FM-1001"
        assert "payment_url" not in billing["invoices"][0]

        token = client.app.state.business.tokens.create(
            {"customer_id": customer_id, "call_id": "call-billing-1"}, ttl_seconds=600
        )
        page = client.get(f"/upload/{token}")
        assert page.status_code == 200
        upload = client.post(
            f"/upload/{token}",
            data={"note": "North wall"},
            files=[("files", ("wall.jpg", b"\xff\xd8\xff\xe0" + b"safe-test-jpeg", "image/jpeg"))],
        )
        assert upload.status_code == 200
        assert "Upload complete" in upload.text
        uploads = client.get("/api/v1/crm/uploads", headers=headers).json()
        assert len(uploads) == 1


def test_campaign_batch_enqueue_reuses_consent_and_skips_duplicates(settings):
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer admin-test-token"}
        for index, phone in enumerate(("+12315550001", "+12315550002"), start=1):
            response = client.post(
                "/api/v1/consents",
                headers=headers,
                json={
                    "customer_id": f"customer-{index}",
                    "phone": phone,
                    "transactional_voice": True,
                    "source": "signed-service-agreement",
                    "text_version": "2026-08",
                    "consent_text": "Automated transactional voice calls are authorized.",
                    "consented_at": "2026-08-01T12:00:00+00:00",
                },
            )
            assert response.status_code == 200

        campaign = client.post(
            "/api/v1/campaigns",
            headers=headers,
            json={
                "name": "Friendly invoice reminders",
                "purpose": "billing_reminder",
                "status": "active",
            },
        ).json()
        payload = {
            "start_at": "2026-08-12T14:00:00+00:00",
            "spacing_seconds": 30,
            "entries": [
                {
                    "phone": "+12315550001",
                    "customer_id": "customer-1",
                    "timezone": "America/Detroit",
                },
                {
                    "phone": "+12315550002",
                    "customer_id": "customer-2",
                    "timezone": "America/Detroit",
                },
            ],
            "common_payload": {"invoice_batch": "august"},
        }
        first = client.post(
            f"/api/v1/campaigns/{campaign['id']}/enqueue",
            headers=headers,
            json=payload,
        )
        assert first.status_code == 200
        result = first.json()
        assert result["created_count"] == 2
        assert result["skipped_count"] == 0
        assert all(item["allowed_now"] is True for item in result["eligibility_previews"])
        scheduled = [datetime.fromisoformat(item["scheduled_for"]) for item in result["created"]]
        assert (scheduled[1] - scheduled[0]).total_seconds() == 30
        assert all(item["payload"]["invoice_batch"] == "august" for item in result["created"])

        duplicate = client.post(
            f"/api/v1/campaigns/{campaign['id']}/enqueue",
            headers=headers,
            json=payload,
        ).json()
        assert duplicate["created_count"] == 0
        assert duplicate["skipped_count"] == 2

        campaigns = client.get("/api/v1/campaigns", headers=headers).json()
        current = next(item for item in campaigns if item["id"] == campaign["id"])
        assert current["total_jobs"] == 2
        assert current["open_jobs"] == 2


def test_post_call_uses_aava_lead_id_for_exact_outbound_job_correlation(settings):
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer admin-test-token"}
        internal = {"X-Internal-Token": "internal-test-token"}
        requested_at = datetime.now(timezone.utc).isoformat()

        first = client.post(
            "/api/v1/outbound/jobs",
            headers=headers,
            json={
                "phone": "+12315550100",
                "purpose": "requested_callback",
                "requested_at": requested_at,
                "agent": "floodman_callback",
            },
        ).json()
        second = client.post(
            "/api/v1/outbound/jobs",
            headers=headers,
            json={
                "phone": "+12315550100",
                "purpose": "requested_callback",
                "requested_at": requested_at,
                "agent": "floodman_callback",
            },
        ).json()
        client.app.state.database.update_job(first["id"], status="answered")
        client.app.state.database.update_job(second["id"], status="answered")

        response = client.post(
            "/internal/ava/call-completed",
            headers=internal,
            json={
                "call_id": "aava-call-exact-1",
                "caller_number": "+12315550100",
                "direction": "outbound",
                "agent": "floodman_callback",
                "campaign_id": "campaign-test",
                "lead_id": first["id"],
                "conversation_id": "conversation-test",
                "duration": 42,
                "outcome": "answered_human",
                "summary": "Customer callback completed.",
                "transcript": [],
            },
        )
        assert response.status_code == 200

        exact = client.get(f"/api/v1/outbound/jobs/{first['id']}", headers=headers).json()
        untouched = client.get(f"/api/v1/outbound/jobs/{second['id']}", headers=headers).json()
        assert exact["status"] == "completed"
        assert untouched["status"] == "answered"

        events = client.get(
            "/api/v1/events?call_id=aava-call-exact-1", headers=headers
        ).json()
        completion = next(item for item in events if item["event_type"] == "job_completed")
        assert completion["payload"]["job_id"] == first["id"]
        assert completion["payload"]["correlation"] == "aava_lead_id"


def test_ava_pre_call_infers_outbound_direction_and_returns_job_context(settings):
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer admin-test-token"}
        internal = {"X-Internal-Token": "internal-test-token"}
        campaign = client.post(
            "/api/v1/campaigns",
            headers=headers,
            json={
                "name": "Estimate context test",
                "purpose": "estimate_followup",
                "agent": "floodman_estimate_followup",
                "status": "active",
            },
        ).json()
        job = client.post(
            "/api/v1/outbound/jobs",
            headers=headers,
            json={
                "phone": "+12315550177",
                "purpose": "estimate_followup",
                "customer_id": "customer-177",
                "campaign_id": campaign["id"],
                "agent": "floodman_estimate_followup",
                "consent": {
                    "phone": "+12315550177",
                    "customer_id": "customer-177",
                    "marketing_voice_written": True,
                    "source": "signed-estimate-form",
                    "text_version": "2026-08",
                    "consent_text": "Artificial-voice marketing follow-up authorized.",
                    "consented_at": "2026-08-01T12:00:00+00:00",
                },
                "payload": {"estimate_id": "estimate-177", "lost_reason": "timing"},
            },
        ).json()

        response = client.post(
            "/internal/ava/pre-call",
            headers=internal,
            json={
                "call_id": "aava-outbound-context-177",
                "caller_number": "+12315550177",
                "campaign_id": campaign["id"],
                "lead_id": job["id"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["direction"] == "outbound"
        assert data["lead_id"] == job["id"]
        assert data["campaign_id"] == campaign["id"]
        assert data["call_purpose"] == "estimate_followup"
        assert data["outbound_job"]["payload"]["estimate_id"] == "estimate-177"


def test_direct_outbound_job_rejects_unknown_campaign(settings):
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/v1/outbound/jobs",
            headers={"Authorization": "Bearer admin-test-token"},
            json={
                "phone": "+12315550188",
                "purpose": "requested_callback",
                "campaign_id": "missing-campaign",
                "requested_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Campaign not found"


async def test_customer_lookup_returns_only_masked_candidates(settings):
    db = Database(settings.database_path)
    customer = db.upsert_customer(
        {"name": "Privacy Person", "phone": "+12315550991", "email": "private@example.test"}
    )
    db.upsert_property(
        {
            "customer_id": customer["id"],
            "address": "123 Secret Street",
            "city": "Traverse City",
            "zip": "49684",
        }
    )
    db.upsert_invoice(
        {
            "customer_id": customer["id"],
            "invoice_number": "PRIVATE-1",
            "amount_due": 900,
            "payment_url": "https://pay.example.test/private",
        }
    )
    service = BusinessOperations(settings, db, RoomflowClient(settings, db))
    result = await service.execute("lookup_customer", {"phone": "+12315550991"})
    assert result["found"] is True
    candidate = result["customers"][0]
    assert candidate["customer_id"] == customer["id"]
    assert candidate["phone_last4"] == "0991"
    assert candidate["name_masked"].startswith("P")
    serialized = str(result).lower()
    assert "private@example.test" not in serialized
    assert "secret street" not in serialized
    assert "private-1" not in serialized
    assert "payment_url" not in serialized
    db.close()


async def test_billing_invoice_must_belong_to_verified_customer_and_url_never_returns(settings):
    db = Database(settings.database_path)
    first = db.upsert_customer({"name": "First Customer", "phone": "+12315550992"})
    second = db.upsert_customer({"name": "Second Customer", "phone": "+12315550993"})
    invoice = db.upsert_invoice(
        {
            "customer_id": first["id"],
            "invoice_number": "FM-PRIVATE",
            "amount_due": 500,
            "payment_url": "https://pay.example.test/secret-token",
        }
    )
    service = BusinessOperations(settings, db, RoomflowClient(settings, db))
    mismatch = await service.execute(
        "get_billing_summary",
        {"customer_id": second["id"], "invoice_id": invoice["id"]},
    )
    assert mismatch["error"] == "invoice_customer_mismatch"
    payment = await service.execute(
        "send_payment_link",
        {"customer_id": first["id"], "invoice_id": invoice["id"], "channel": "sms"},
    )
    assert payment["has_payment_link"] is True
    assert "payment_url" not in payment
    assert "secret-token" not in str(payment)
    db.close()


async def test_pre_call_context_filters_campaign_attestation_metadata(settings):
    from app.models import OutboundJobCreate, OutboundPurpose

    db = Database(settings.database_path)
    job = db.create_outbound_job(
        OutboundJobCreate(
            phone="+12315550994",
            purpose=OutboundPurpose.WINBACK,
            payload={
                "estimate_id": "estimate-1",
                "lost_reason": "timing",
                "dnc_status": "clear",
                "dnc_checked_at": "2026-08-11T12:00:00+00:00",
                "reassignment_status": "not_reassigned",
                "internal_admin_note": "do not expose",
            },
        ),
        "floodman_winback",
    )
    result = await RoomflowClient(settings, db).pre_call_context(
        "call-pre-1", job["phone"], "outbound", lead_id=job["id"]
    )
    payload = result.data["outbound_job"]["payload"]
    assert payload["estimate_id"] == "estimate-1"
    assert payload["lost_reason"] == "timing"
    assert "dnc_status" not in payload
    assert "reassignment_status" not in payload
    assert "internal_admin_note" not in payload
    db.close()


def test_verified_tool_envelope_cannot_switch_customer_inside_model_payload(settings):
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        headers = {"Authorization": "Bearer admin-test-token"}
        internal = {"X-Internal-Token": "internal-test-token"}
        first = client.post(
            "/api/v1/crm/customers",
            headers=headers,
            json={"name": "Verified Person", "phone": "+12315550661"},
        ).json()
        second = client.post(
            "/api/v1/crm/customers",
            headers=headers,
            json={"name": "Target Person", "phone": "+12315550662"},
        ).json()
        client.post(
            "/api/v1/crm/properties",
            headers=headers,
            json={"customer_id": first["id"], "address": "661 Verified Road", "zip": "49684"},
        )
        target_invoice = client.post(
            "/api/v1/crm/invoices",
            headers=headers,
            json={
                "customer_id": second["id"],
                "invoice_number": "TARGET-PRIVATE",
                "amount_due": 777,
                "payment_url": "https://pay.example.test/target-secret",
            },
        ).json()
        verification = client.post(
            "/internal/tools/verify-customer",
            headers=internal,
            json={
                "call_id": "call-envelope-1",
                "customer_id": first["id"],
                "caller_number": "+12315550661",
                "data": {
                    "customer_id": first["id"],
                    "phone": "+12315550661",
                    "name": "Verified Person",
                    "street_number": "661",
                    "zip": "49684",
                },
            },
        ).json()
        assert verification["verified"] is True
        attempted_switch = client.post(
            "/internal/tools/get-billing-summary",
            headers=internal,
            json={
                "call_id": "call-envelope-1",
                "customer_id": first["id"],
                "caller_number": "+12315550661",
                "data": {"customer_id": second["id"], "invoice_id": target_invoice["id"]},
            },
        ).json()
        assert attempted_switch["error"] == "invoice_customer_mismatch"
        assert "target-private" not in str(attempted_switch).lower()


def test_upload_rejects_mime_spoofing(settings):
    from app.main import create_app

    with TestClient(create_app(settings)) as client:
        token = client.app.state.business.tokens.create(
            {"customer_id": "customer-upload-test", "call_id": "call-upload-test"},
            ttl_seconds=600,
        )
        response = client.post(
            f"/upload/{token}",
            files=[("files", ("fake.jpg", b"this-is-not-a-jpeg", "image/jpeg"))],
        )
        assert response.status_code == 415
        assert response.json()["detail"] == "File contents do not match the declared type"
        assert not list((settings.data_dir / "uploads").glob("*"))
