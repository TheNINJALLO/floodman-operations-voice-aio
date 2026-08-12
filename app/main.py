from __future__ import annotations

import asyncio
import html
import hashlib
import hmac
import logging
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.ava.agents import AGENTS
from app.ava.bootstrap import provision_agents
from app.business import BusinessOperations
from app.call_gate.audio_socket import AudioSocketGateServer
from app.call_gate.classifier import CallGateClassifier
from app.call_gate.transcribers import build_transcriber
from app.compliance.engine import ComplianceEngine, normalize_phone
from app.config import Settings
from app.db import Database
from app.diagnostics import collect_diagnostics
from app.models import (
    CallCompletedEvent,
    CampaignCreate,
    CampaignEnqueueRequest,
    CampaignUpdate,
    ConsentSnapshot,
    CustomerUpsert,
    EligibilityRequest,
    GateClassificationRequest,
    GateDecision,
    GateRegistration,
    GateState,
    InvoiceUpsert,
    JobStatus,
    OutboundJobCreate,
    OutboundPurpose,
    PropertyUpsert,
    RoomflowToolRequest,
    SuppressionCreate,
    TestCallRequest,
)
from app.outbound.ami import AMIClient
from app.outbound.worker import OutboundWorker
from app.roomflow.client import RoomflowClient

logger = logging.getLogger(__name__)
VERSION = "1.1.1"

PURPOSE_AGENT = {
    OutboundPurpose.REQUESTED_CALLBACK: "callback_agent",
    OutboundPurpose.MISSED_CALL_CALLBACK: "callback_agent",
    OutboundPurpose.BILLING_REMINDER: "billing_agent",
    OutboundPurpose.PAYMENT_FOLLOWUP: "billing_agent",
    OutboundPurpose.ESTIMATE_FOLLOWUP: "estimate_followup_agent",
    OutboundPurpose.CANCELED_INSPECTION: "estimate_followup_agent",
    OutboundPurpose.WINBACK: "winback_agent",
    OutboundPurpose.MAINTENANCE: "winback_agent",
}

LIVE_CONTACT_OUTCOMES = {
    "answered",
    "connected",
    "completed",
    "appointment_booked",
    "payment_link_sent",
    "callback_requested",
    "not_interested",
    "opted_out",
}


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _bearer(value: str | None) -> str:
    if not value:
        return ""
    return value[7:].strip() if value.lower().startswith("bearer ") else value.strip()


def _safe_limit(value: int, maximum: int = 1000) -> int:
    return min(max(value, 1), maximum)


def _safe_filename(value: str) -> str:
    basename = Path(value or "upload.bin").name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", basename).strip("._")
    return (cleaned or "upload.bin")[:180]


def _matches_upload_signature(content_type: str, header: bytes) -> bool:
    signatures = {
        "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
        "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/webp": lambda value: len(value) >= 12 and value[:4] == b"RIFF" and value[8:12] == b"WEBP",
        "application/pdf": lambda value: value.startswith(b"%PDF-"),
        "image/heic": lambda value: len(value) >= 12
        and value[4:8] == b"ftyp"
        and value[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"},
    }
    validator = signatures.get(content_type)
    return bool(validator and validator(header))


def _upload_page(title: str, message: str, *, success: bool = False, token: str = "") -> str:
    safe_title = html.escape(title, quote=True)
    safe_message = html.escape(message, quote=True)
    safe_token = html.escape(token, quote=True)
    button = (
        f'<form action="/upload/{safe_token}" method="post" enctype="multipart/form-data">'
        '<label>Property photos or documents</label>'
        '<input type="file" name="files" accept="image/*,application/pdf" multiple required>'
        '<label>Optional note</label><textarea name="note" rows="4" placeholder="Tell us what the photos show"></textarea>'
        '<button type="submit">Send to Floodman</button></form>'
        if token and not success
        else ""
    )
    state_class = "success" if success else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{safe_title}</title><style>
:root{{color-scheme:dark}}*{{box-sizing:border-box}}body{{margin:0;background:#07111d;color:#e9f2f7;font-family:Inter,system-ui,sans-serif;min-height:100vh;display:grid;place-items:center;padding:24px}}
main{{width:min(680px,100%);background:#0d1c2a;border:1px solid #21425a;border-radius:22px;padding:28px;box-shadow:0 24px 70px #0008}}h1{{margin:0 0 8px;font-size:clamp(1.8rem,5vw,2.8rem)}}p{{color:#b9cbd6;line-height:1.55}}label{{display:block;margin:20px 0 8px;font-weight:700}}input,textarea{{width:100%;background:#07111d;color:#fff;border:1px solid #315a73;border-radius:12px;padding:13px}}button{{width:100%;margin-top:20px;border:0;border-radius:12px;padding:14px 18px;font-weight:800;background:#38bdf8;color:#032033;cursor:pointer}}.brand{{font-weight:900;letter-spacing:.08em;color:#7dd3fc}}.success{{color:#86efac}}</style></head>
<body><main><div class="brand">FLOODMAN</div><h1 class="{state_class}">{safe_title}</h1><p>{safe_message}</p>{button}</main></body></html>"""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(settings.database_path)
        roomflow = RoomflowClient(settings, database)
        app.state.settings = settings
        app.state.database = database
        app.state.roomflow = roomflow
        app.state.business = BusinessOperations(settings, database, roomflow)
        app.state.compliance = ComplianceEngine(settings, database)
        app.state.ami = AMIClient(settings)
        app.state.classifier = CallGateClassifier(settings)
        app.state.gate_server = None
        app.state.worker = None
        app.state.worker_task = None

        try:
            provisioned = provision_agents(settings) if settings.reconcile_ava_agents else []
            logger.info("AVA agent provisioning complete: %s", ", ".join(provisioned) or "no changes")
        except Exception:
            logger.exception("AVA agent provisioning failed; control plane will continue")

        if settings.gate_enabled:
            try:
                transcriber = build_transcriber(settings)
                gate_server = AudioSocketGateServer(settings, database, transcriber)
                await gate_server.start()
                app.state.gate_server = gate_server
            except Exception:
                logger.exception("Call gate startup failed; inbound dialplan will fail open")

        worker = OutboundWorker(settings, database, app.state.compliance, app.state.ami, roomflow)
        app.state.worker = worker
        app.state.worker_task = asyncio.create_task(worker.run(), name="floodman-outbound-worker")

        try:
            yield
        finally:
            await worker.stop()
            if app.state.worker_task:
                app.state.worker_task.cancel()
                await asyncio.gather(app.state.worker_task, return_exceptions=True)
            if app.state.gate_server:
                await app.state.gate_server.stop()
            database.close()

    app = FastAPI(
        title=settings.app_name,
        version=VERSION,
        description="Floodman AI phone operations controller for Asterisk and AVA",
        lifespan=lifespan,
        docs_url="/docs" if settings.enable_api_docs else None,
        redoc_url="/redoc" if settings.enable_api_docs else None,
        openapi_url="/openapi.json" if settings.enable_api_docs else None,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))

    @app.middleware("http")
    async def production_security(request: Request, call_next):
        if (
            settings.force_https
            and request.url.scheme != "https"
            and request.url.path not in {"/livez", "/readyz", "/health"}
        ):
            target = request.url.replace(scheme="https")
            return RedirectResponse(str(target), status_code=307)
        response = await call_next(request)
        if settings.security_headers_enabled:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
            response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'self'; form-action 'self'",
            )
            if request.url.scheme == "https":
                response.headers.setdefault(
                    "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
                )
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    async def require_admin(
        authorization: str | None = Header(default=None),
        x_admin_token: str | None = Header(default=None),
    ) -> None:
        supplied = _bearer(authorization) or (x_admin_token or "")
        if not supplied or not hmac.compare_digest(supplied, settings.admin_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid admin token")

    async def require_internal(x_internal_token: str | None = Header(default=None)) -> None:
        supplied = x_internal_token or ""
        if not supplied or not hmac.compare_digest(supplied, settings.internal_token):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid internal token")

    @app.get("/livez", include_in_schema=False)
    async def livez() -> dict[str, Any]:
        return {"ok": True, "service": settings.app_name, "version": VERSION}

    async def diagnostic_payload(request: Request, *, include_network: bool) -> dict[str, Any]:
        database: Database = request.app.state.database
        result = await collect_diagnostics(
            settings,
            database=database,
            gate_server=request.app.state.gate_server,
            ami=request.app.state.ami,
            include_network=include_network,
        )
        result.update(
            {
                "service": settings.app_name,
                "tagline": "Never Miss A Call Again With Floodman’s 24/7 AI Receptionist",
                "version": VERSION,
                "environment": settings.environment,
                "counts": database.dashboard_counts(),
            }
        )
        return result

    @app.get("/health", include_in_schema=False)
    async def health(request: Request) -> dict[str, Any]:
        payload = await diagnostic_payload(request, include_network=False)
        return {
            "ok": bool(payload["ok"]),
            "service": settings.app_name,
            "version": VERSION,
        }

    @app.get("/readyz", include_in_schema=False)
    async def readyz(request: Request) -> JSONResponse:
        payload = await diagnostic_payload(request, include_network=True)
        public_payload = {
            "ready": bool(payload["ready"]),
            "service": settings.app_name,
            "version": VERSION,
        }
        return JSONResponse(status_code=200 if payload["ready"] else 503, content=public_payload)

    @app.get("/api/v1/diagnostics", dependencies=[Depends(require_admin)])
    async def diagnostics(request: Request) -> dict[str, Any]:
        return await diagnostic_payload(request, include_network=True)

    @app.get("/api/v1/dashboard", dependencies=[Depends(require_admin)])
    async def dashboard(request: Request) -> dict[str, Any]:
        database: Database = request.app.state.database
        diagnostics = await diagnostic_payload(request, include_network=False)
        roomflow_configured = bool(
            settings.roomflow_enabled
            and settings.roomflow_base_url
            and settings.roomflow_token
            and settings.roomflow_endpoints
        )
        return {
            "counts": database.dashboard_counts(),
            "recent_gate_sessions": database.list_gate_sessions(20),
            "recent_outbound_jobs": database.list_outbound_jobs(20),
            "recent_events": database.list_call_events(30),
            "campaigns": database.list_campaigns(),
            # Stable summary consumed by the web dashboard. The full diagnostics
            # document remains available separately for operators and automation.
            "components": {
                "gate": {
                    "enabled": settings.gate_enabled,
                    "listening": request.app.state.gate_server is not None,
                    "port": settings.gate_port,
                    "transcriber": settings.gate_transcriber,
                },
                "outbound": {
                    "enabled": settings.outbound_enabled,
                    "ami_enabled": settings.ami_enabled,
                    "test_calls_enabled": settings.test_calls_enabled,
                    "concurrency": settings.outbound_concurrency,
                },
                "roomflow": {
                    "enabled": settings.roomflow_enabled,
                    "configured": roomflow_configured,
                    "endpoint_mappings": len(settings.roomflow_endpoints),
                },
                "local_crm": {"enabled": settings.local_crm_enabled},
            },
            "diagnostics": diagnostics,
        }

    @app.get("/api/v1/system", dependencies=[Depends(require_admin)])
    async def system_status(request: Request) -> dict[str, Any]:
        return {
            "version": VERSION,
            "data_dir": str(settings.data_dir),
            "database_path": str(settings.database_path),
            "agents_db_path": str(settings.agents_db_path),
            "public_base_url": settings.public_base_url,
            "timezone": settings.timezone,
            "agents": [
                {
                    "slug": agent.slug,
                    "display_name": agent.display_name,
                    "role": agent.role_label,
                    "tools": list(agent.tools),
                }
                for agent in AGENTS
            ],
            "roomflow_operations": sorted(settings.roomflow_endpoints),
            "transfer_destinations": settings.transfer_destinations,
            "service_information": settings.service_information,
            "scheduling": settings.scheduling_config,
            "compliance": settings.compliance_config,
            "health": await diagnostic_payload(request, include_network=False),
        }

    # Call gate ---------------------------------------------------------
    @app.post("/api/v1/gate/classify", response_model=GateDecision, dependencies=[Depends(require_admin)])
    async def classify_gate(request: Request, payload: GateClassificationRequest) -> GateDecision:
        return request.app.state.classifier.classify(payload)

    @app.get("/api/v1/gate/sessions", dependencies=[Depends(require_admin)])
    async def gate_sessions(request: Request, limit: int = 100) -> list[dict[str, Any]]:
        return request.app.state.database.list_gate_sessions(_safe_limit(limit, 500))

    @app.post("/internal/gate/start", dependencies=[Depends(require_internal)])
    async def gate_start(request: Request, payload: GateRegistration) -> dict[str, Any]:
        gate_uuid = payload.gate_uuid or str(uuid.uuid4())
        request.app.state.database.register_gate(payload, gate_uuid)
        return {
            "ok": True,
            "gate_uuid": gate_uuid,
            "gate_enabled": bool(request.app.state.gate_server),
            "default_agent": settings.default_agent,
            "default_provider": settings.default_provider,
        }

    @app.get("/internal/gate/decision/{gate_uuid}", dependencies=[Depends(require_internal)])
    async def gate_decision(request: Request, gate_uuid: str) -> dict[str, Any]:
        row = request.app.state.database.get_gate(gate_uuid)
        if not row:
            return {
                "ready": True,
                "state": GateState.FAILED.value,
                "classification": "unknown",
                "agent": settings.default_agent,
                "provider": settings.default_provider,
                "opening_transcript": "",
                "reason": "gate_session_not_found_fail_open",
            }
        ready = row.get("state") in {
            GateState.READY.value,
            GateState.SECURITY_BLOCK.value,
            GateState.TIMEOUT.value,
            GateState.FAILED.value,
        }
        return {
            "ready": ready,
            "state": row.get("state"),
            "classification": row.get("classification") or "unknown",
            "confidence": row.get("confidence") or 0,
            "agent": row.get("agent") or settings.default_agent,
            "provider": row.get("provider") or settings.default_provider,
            "opening_transcript": row.get("opening_transcript") or "",
            "announcement_detected": bool(row.get("announcement_detected")),
            "reason": row.get("reason") or "",
        }

    # Campaigns and outbound -------------------------------------------
    @app.post("/api/v1/test-calls/outbound", dependencies=[Depends(require_admin)])
    async def place_test_call(request: Request, payload: TestCallRequest) -> dict[str, Any]:
        if not settings.test_calls_enabled:
            raise HTTPException(status_code=409, detail="TEST_CALLS_ENABLED is false")
        if not settings.ami_enabled:
            raise HTTPException(status_code=409, detail="AMI_ENABLED must be true for test calls")
        allowlist = {normalize_phone(value) for value in settings.test_call_allowlist if value}
        if not allowlist or payload.phone not in allowlist:
            raise HTTPException(
                status_code=403,
                detail="Destination is not present in TEST_CALL_ALLOWLIST",
            )
        result = await request.app.state.ami.originate_test_call(
            phone=payload.phone,
            label=payload.label,
        )
        request.app.state.database.add_call_event(
            result.action_id,
            "outbound",
            "production_test_call",
            {
                "destination": payload.phone,
                "label": payload.label,
                "queued": result.ok,
                "answered": result.answered,
                "channel": result.channel,
                "reason_code": result.reason_code,
            },
        )
        return {
            "ok": result.ok,
            "action_id": result.action_id,
            "message": result.message,
            "answered": result.answered,
            "channel": result.channel,
            "reason_code": result.reason_code,
            "instructions": "Answer the call, speak after the echo-test prompt, and confirm you hear your voice returned.",
        }

    @app.post("/api/v1/campaigns", dependencies=[Depends(require_admin)])
    async def create_campaign(request: Request, payload: CampaignCreate) -> dict[str, Any]:
        agent = payload.agent or getattr(settings, PURPOSE_AGENT[payload.purpose])
        return request.app.state.database.create_campaign(
            payload.name, payload.purpose.value, agent, payload.status, payload.config
        )

    @app.get("/api/v1/campaigns", dependencies=[Depends(require_admin)])
    async def list_campaigns(request: Request) -> list[dict[str, Any]]:
        return request.app.state.database.list_campaigns()

    @app.post("/api/v1/campaigns/{campaign_id}/enqueue", dependencies=[Depends(require_admin)])
    async def enqueue_campaign(
        request: Request, campaign_id: str, payload: CampaignEnqueueRequest
    ) -> dict[str, Any]:
        database: Database = request.app.state.database
        campaign = database.get_campaign(campaign_id)
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")
        try:
            purpose = OutboundPurpose(str(campaign["purpose"]))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Campaign purpose is invalid") from exc

        start_at = payload.start_at
        if start_at.tzinfo is None:
            start_at = start_at.replace(tzinfo=timezone.utc)

        created: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        previews: list[dict[str, Any]] = []
        for index, entry in enumerate(payload.entries):
            phone = normalize_phone(entry.phone)
            if database.has_open_campaign_job(campaign_id, phone):
                skipped.append({"phone": phone, "reason": "open_campaign_job_exists"})
                continue
            consent = entry.consent
            if consent is None:
                cached = database.get_consent(phone)
                if cached:
                    consent = ConsentSnapshot.model_validate(cached)
            scheduled_for = entry.scheduled_for or (
                start_at + timedelta(seconds=index * payload.spacing_seconds)
            )
            merged_payload = {
                **payload.common_payload,
                **entry.payload,
                "source": "campaign",
                "campaign_id": campaign_id,
                "campaign_name": campaign.get("name") or "",
            }
            job_request = OutboundJobCreate(
                phone=phone,
                purpose=purpose,
                customer_id=entry.customer_id,
                campaign_id=campaign_id,
                timezone=entry.timezone or settings.timezone,
                scheduled_for=scheduled_for,
                requested_at=entry.requested_at,
                max_attempts=payload.max_attempts,
                agent=str(campaign.get("agent") or getattr(settings, PURPOSE_AGENT[purpose])),
                consent=consent,
                payload=merged_payload,
            )
            job = database.create_outbound_job(job_request, job_request.agent)
            eligibility = request.app.state.compliance.evaluate(
                EligibilityRequest(
                    phone=job_request.phone,
                    purpose=job_request.purpose,
                    customer_id=job_request.customer_id,
                    timezone=job_request.timezone,
                    scheduled_for=job_request.scheduled_for,
                    requested_at=job_request.requested_at,
                    consent=job_request.consent,
                    metadata=job_request.payload,
                )
            )
            created.append(job)
            previews.append(
                {
                    "job_id": job.get("id"),
                    "phone": phone,
                    "allowed_now": eligibility.allowed,
                    "reason": eligibility.reason,
                    "retry_at": eligibility.retry_at,
                }
            )
        return {
            "ok": True,
            "campaign": campaign,
            "created_count": len(created),
            "skipped_count": len(skipped),
            "created": created,
            "skipped": skipped,
            "eligibility_previews": previews,
            "note": "Every job is evaluated again immediately before Asterisk dials it.",
        }

    @app.patch("/api/v1/campaigns/{campaign_id}", dependencies=[Depends(require_admin)])
    async def update_campaign(
        request: Request, campaign_id: str, payload: CampaignUpdate
    ) -> dict[str, Any]:
        row = request.app.state.database.update_campaign(
            campaign_id, **payload.model_dump(exclude_none=True)
        )
        if not row:
            raise HTTPException(status_code=404, detail="Campaign not found")
        return row

    @app.post("/api/v1/outbound/eligibility", dependencies=[Depends(require_admin)])
    async def preview_eligibility(request: Request, payload: EligibilityRequest) -> dict[str, Any]:
        return request.app.state.compliance.evaluate(payload).model_dump(mode="json")

    @app.post("/api/v1/outbound/jobs", dependencies=[Depends(require_admin)])
    async def create_outbound_job(request: Request, payload: OutboundJobCreate) -> dict[str, Any]:
        payload.phone = normalize_phone(payload.phone)
        if payload.campaign_id and not request.app.state.database.get_campaign(payload.campaign_id):
            raise HTTPException(status_code=404, detail="Campaign not found")
        agent = payload.agent or getattr(settings, PURPOSE_AGENT[payload.purpose])
        if payload.consent:
            request.app.state.database.upsert_consent(payload.consent.model_dump(mode="json"))
        job = request.app.state.database.create_outbound_job(payload, agent)
        eligibility = request.app.state.compliance.evaluate(
            EligibilityRequest(
                phone=payload.phone,
                purpose=payload.purpose,
                customer_id=payload.customer_id,
                timezone=payload.timezone,
                scheduled_for=payload.scheduled_for,
                requested_at=payload.requested_at,
                consent=payload.consent,
                metadata=payload.payload,
            )
        )
        job["eligibility_preview"] = eligibility.model_dump(mode="json")
        return job

    @app.get("/api/v1/outbound/jobs", dependencies=[Depends(require_admin)])
    async def list_outbound_jobs(request: Request, limit: int = 200) -> list[dict[str, Any]]:
        return request.app.state.database.list_outbound_jobs(_safe_limit(limit))

    @app.get("/api/v1/outbound/jobs/{job_id}", dependencies=[Depends(require_admin)])
    async def get_outbound_job(request: Request, job_id: str) -> dict[str, Any]:
        row = request.app.state.database.get_outbound_job(job_id)
        if not row:
            raise HTTPException(status_code=404, detail="Outbound job not found")
        return row

    @app.post("/api/v1/outbound/jobs/{job_id}/cancel", dependencies=[Depends(require_admin)])
    async def cancel_outbound_job(request: Request, job_id: str) -> dict[str, Any]:
        if not request.app.state.database.get_outbound_job(job_id):
            raise HTTPException(status_code=404, detail="Outbound job not found")
        return request.app.state.database.update_job(
            job_id, status=JobStatus.CANCELED.value, last_error="canceled_by_admin"
        ) or {}

    @app.post("/api/v1/outbound/jobs/{job_id}/retry", dependencies=[Depends(require_admin)])
    async def retry_outbound_job(request: Request, job_id: str) -> dict[str, Any]:
        row = request.app.state.database.retry_job(job_id)
        if not row:
            raise HTTPException(status_code=404, detail="Outbound job not found")
        return row

    # Consent and suppression ------------------------------------------
    @app.post("/api/v1/consents", dependencies=[Depends(require_admin)])
    async def upsert_consent(request: Request, payload: ConsentSnapshot) -> dict[str, Any]:
        return request.app.state.database.upsert_consent(payload.model_dump(mode="json"))

    @app.get("/api/v1/consents", dependencies=[Depends(require_admin)])
    async def list_consents(request: Request, limit: int = 500) -> list[dict[str, Any]]:
        return request.app.state.database.list_consents(_safe_limit(limit, 2000))

    @app.post("/api/v1/consents/{phone}/revoke", dependencies=[Depends(require_admin)])
    async def revoke_consent(
        request: Request, phone: str, categories: str = "all"
    ) -> dict[str, Any]:
        normalized = normalize_phone(phone)
        row = request.app.state.database.revoke_consent(
            normalized, [item.strip() for item in categories.split(",") if item.strip()]
        )
        if not row:
            raise HTTPException(status_code=404, detail="Consent record not found")
        return row

    @app.post("/api/v1/suppressions", dependencies=[Depends(require_admin)])
    async def create_suppression(request: Request, payload: SuppressionCreate) -> dict[str, Any]:
        return request.app.state.database.suppress(
            normalize_phone(payload.phone), payload.reason, payload.categories, payload.source
        )

    @app.get("/api/v1/suppressions", dependencies=[Depends(require_admin)])
    async def list_suppressions(request: Request) -> list[dict[str, Any]]:
        return request.app.state.database.list_suppressions()

    @app.delete("/api/v1/suppressions/{phone}", dependencies=[Depends(require_admin)])
    async def delete_suppression(request: Request, phone: str) -> dict[str, Any]:
        return {"ok": request.app.state.database.delete_suppression(normalize_phone(phone))}

    # Local CRM ---------------------------------------------------------
    @app.post("/api/v1/crm/customers", dependencies=[Depends(require_admin)])
    async def upsert_customer(request: Request, payload: CustomerUpsert) -> dict[str, Any]:
        return request.app.state.database.upsert_customer(payload.model_dump(mode="json"))

    @app.get("/api/v1/crm/customers", dependencies=[Depends(require_admin)])
    async def list_customers(
        request: Request,
        phone: str = "",
        name: str = "",
        address: str = "",
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        if phone or name or address:
            return request.app.state.database.search_customers(
                phone=normalize_phone(phone) if phone else "",
                name=name,
                address=address,
                limit=_safe_limit(limit),
            )
        return request.app.state.database.list_customers(_safe_limit(limit))

    @app.get("/api/v1/crm/customers/{customer_id}", dependencies=[Depends(require_admin)])
    async def customer_bundle(request: Request, customer_id: str) -> dict[str, Any]:
        row = request.app.state.database.customer_bundle(customer_id)
        if not row:
            raise HTTPException(status_code=404, detail="Customer not found")
        return row

    @app.post("/api/v1/crm/properties", dependencies=[Depends(require_admin)])
    async def upsert_property(request: Request, payload: PropertyUpsert) -> dict[str, Any]:
        if not request.app.state.database.get_customer(payload.customer_id):
            raise HTTPException(status_code=400, detail="Customer does not exist")
        return request.app.state.database.upsert_property(payload.model_dump(mode="json"))

    @app.get("/api/v1/crm/properties", dependencies=[Depends(require_admin)])
    async def list_properties(
        request: Request, customer_id: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        return request.app.state.database.list_properties(customer_id, _safe_limit(limit))

    @app.post("/api/v1/crm/invoices", dependencies=[Depends(require_admin)])
    async def upsert_invoice(request: Request, payload: InvoiceUpsert) -> dict[str, Any]:
        if not request.app.state.database.get_customer(payload.customer_id):
            raise HTTPException(status_code=400, detail="Customer does not exist")
        return request.app.state.database.upsert_invoice(payload.model_dump(mode="json"))

    @app.get("/api/v1/crm/invoices", dependencies=[Depends(require_admin)])
    async def list_invoices(
        request: Request, customer_id: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        return request.app.state.database.list_invoices(customer_id, _safe_limit(limit))

    @app.get("/api/v1/crm/leads", dependencies=[Depends(require_admin)])
    async def list_leads(
        request: Request, customer_id: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        return request.app.state.database.list_leads(customer_id, _safe_limit(limit))

    @app.get("/api/v1/crm/appointments", dependencies=[Depends(require_admin)])
    async def list_appointments(
        request: Request, customer_id: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        return request.app.state.database.list_appointments(customer_id, _safe_limit(limit))

    @app.get("/api/v1/crm/callbacks", dependencies=[Depends(require_admin)])
    async def list_callbacks(
        request: Request, customer_id: str = "", task_status: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        return request.app.state.database.list_callback_tasks(
            customer_id, task_status, _safe_limit(limit)
        )

    @app.get("/api/v1/crm/uploads", dependencies=[Depends(require_admin)])
    async def list_uploads(
        request: Request, customer_id: str = "", limit: int = 200
    ) -> list[dict[str, Any]]:
        return request.app.state.database.list_uploads(customer_id, _safe_limit(limit))

    @app.get("/api/v1/events", dependencies=[Depends(require_admin)])
    async def list_events(
        request: Request, limit: int = 200, call_id: str = ""
    ) -> list[dict[str, Any]]:
        return request.app.state.database.list_call_events(_safe_limit(limit), call_id)

    @app.get("/api/v1/outbox", dependencies=[Depends(require_admin)])
    async def list_outbox(request: Request, limit: int = 500) -> list[dict[str, Any]]:
        return request.app.state.database.list_outbox(_safe_limit(limit, 2000))

    @app.post("/api/v1/outbox/{item_id}/retry", dependencies=[Depends(require_admin)])
    async def retry_outbox(request: Request, item_id: str) -> dict[str, Any]:
        return {"ok": request.app.state.database.retry_outbox(item_id)}

    # AVA phase hooks ---------------------------------------------------
    @app.post("/internal/ava/pre-call", dependencies=[Depends(require_internal)])
    async def ava_pre_call(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
        campaign_id = str(payload.get("campaign_id") or "")
        lead_id = str(payload.get("lead_id") or "")
        raw_direction = str(payload.get("direction") or "").strip().lower()
        direction = raw_direction if raw_direction in {"inbound", "outbound"} else (
            "outbound" if campaign_id or lead_id else "inbound"
        )
        result = await request.app.state.roomflow.pre_call_context(
            str(payload.get("call_id") or ""),
            normalize_phone(str(payload.get("caller_number") or "")),
            direction,
            campaign_id,
            lead_id,
        )
        return result.data

    @app.post("/internal/ava/call-completed", dependencies=[Depends(require_internal)])
    async def ava_call_completed(request: Request, payload: CallCompletedEvent) -> dict[str, Any]:
        database: Database = request.app.state.database
        event_payload = payload.model_dump(mode="json")
        database.add_call_event(payload.call_id, payload.direction, "call_completed", event_payload)
        if payload.direction == "outbound" and payload.caller_number:
            phone = normalize_phone(payload.caller_number)
            completed_job = None
            # Floodman sets each outbound job ID as AVA's AAVA_LEAD_ID. This gives
            # post-call hooks exact correlation even when several calls target the
            # same customer number close together.
            if payload.lead_id:
                completed_job = database.complete_outbound_job(
                    payload.lead_id,
                    payload.outcome or "completed",
                    payload.call_id,
                )
            if completed_job is None:
                completed_job = database.complete_latest_job_for_phone(
                    phone,
                    payload.outcome or "completed",
                    payload.call_id,
                )
            live_contact = bool(payload.metadata.get("live_contact")) or (
                payload.duration >= 15 and (payload.outcome or "").lower() in LIVE_CONTACT_OUTCOMES
            )
            if live_contact:
                database.add_call_event(
                    payload.call_id,
                    "outbound",
                    "live_contact",
                    {"phone": phone, "outcome": payload.outcome, "duration": payload.duration},
                )
        business_result = await request.app.state.business.execute(
            "record_call_outcome",
            event_payload,
            call_id=payload.call_id,
            caller_number=payload.caller_number,
            idempotency_key=f"call-outcome:{payload.call_id}",
        )
        return {"ok": True, "business": business_result}

    # In-call business tools -------------------------------------------
    async def execute_business_tool(
        request: Request,
        operation: str,
        payload: RoomflowToolRequest,
        *,
        protected: bool = False,
    ) -> dict[str, Any]:
        if protected:
            if not payload.call_id or not payload.customer_id:
                return {
                    "ok": False,
                    "operation": operation,
                    "error": "identity_verification_required",
                    "safe_message": "The account must be verified before I can access that information.",
                }
            if not request.app.state.database.is_verified(payload.call_id, payload.customer_id):
                return {
                    "ok": False,
                    "operation": operation,
                    "error": "identity_verification_required",
                    "safe_message": "The account must be verified before I can access that information.",
                }
        return await request.app.state.business.execute(
            operation,
            payload.data,
            call_id=payload.call_id,
            caller_number=payload.caller_number,
            customer_id=payload.customer_id,
            idempotency_key=payload.idempotency_key,
        )

    @app.post("/internal/tools/lookup-customer", dependencies=[Depends(require_internal)])
    async def tool_lookup_customer(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "lookup_customer", payload)

    @app.post("/internal/tools/verify-customer", dependencies=[Depends(require_internal)])
    async def tool_verify_customer(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "verify_customer_identity", payload)

    @app.post("/internal/tools/create-lead", dependencies=[Depends(require_internal)])
    async def tool_create_lead(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "create_lead", payload)

    @app.post("/internal/tools/create-emergency-case", dependencies=[Depends(require_internal)])
    async def tool_create_emergency(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "create_emergency_case", payload)

    @app.post("/internal/tools/check-availability", dependencies=[Depends(require_internal)])
    async def tool_check_availability(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "check_availability", payload)

    @app.post("/internal/tools/schedule-inspection", dependencies=[Depends(require_internal)])
    async def tool_schedule_inspection(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "schedule_inspection", payload)

    @app.post("/internal/tools/reschedule-inspection", dependencies=[Depends(require_internal)])
    async def tool_reschedule_inspection(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "reschedule_inspection", payload, protected=True)

    @app.post("/internal/tools/send-photo-upload-link", dependencies=[Depends(require_internal)])
    async def tool_photo_link(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "send_photo_upload_link", payload)

    @app.post("/internal/tools/get-billing-summary", dependencies=[Depends(require_internal)])
    async def tool_billing_summary(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "get_billing_summary", payload, protected=True)

    @app.post("/internal/tools/send-payment-link", dependencies=[Depends(require_internal)])
    async def tool_payment_link(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "send_payment_link", payload, protected=True)

    @app.post("/internal/tools/create-callback-task", dependencies=[Depends(require_internal)])
    async def tool_callback_task(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        return await execute_business_tool(request, "create_callback_task", payload)

    @app.post("/internal/tools/record-disposition", dependencies=[Depends(require_internal)])
    async def tool_disposition(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        request.app.state.database.add_call_event(
            payload.call_id or str(uuid.uuid4()),
            str(payload.data.get("direction") or "inbound"),
            "disposition",
            payload.data,
        )
        return await execute_business_tool(request, "record_call_outcome", payload)

    @app.post("/internal/tools/opt-out", dependencies=[Depends(require_internal)])
    async def tool_opt_out(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        phone = normalize_phone(str(payload.data.get("phone") or payload.caller_number or ""))
        categories_raw = payload.data.get("categories", "all")
        categories = (
            [item.strip() for item in categories_raw.split(",") if item.strip()]
            if isinstance(categories_raw, str)
            else [str(item) for item in categories_raw]
            if isinstance(categories_raw, list)
            else ["all"]
        )
        suppression = request.app.state.database.suppress(
            phone,
            str(payload.data.get("reason") or "customer_request"),
            categories or ["all"],
            "voice",
        )
        result = await request.app.state.business.execute(
            "record_opt_out",
            {"phone": phone, "categories": categories, "reason": suppression["reason"]},
            call_id=payload.call_id,
            caller_number=phone,
            idempotency_key=f"optout:{phone}:{','.join(sorted(categories))}",
        )
        return {"ok": True, "suppression": suppression, "business": result}

    @app.post("/internal/tools/security-event", dependencies=[Depends(require_internal)])
    async def tool_security_event(request: Request, payload: RoomflowToolRequest) -> dict[str, Any]:
        call_id = payload.call_id or str(uuid.uuid4())
        request.app.state.database.add_call_event(call_id, "inbound", "security_event", payload.data)
        result = await request.app.state.business.execute(
            "record_security_event",
            {"call_id": call_id, "caller_number": payload.caller_number, **payload.data},
            call_id=call_id,
            caller_number=payload.caller_number,
            idempotency_key=payload.idempotency_key or f"security:{call_id}",
        )
        return {"ok": True, "business": result}

    @app.post("/internal/tools/public-business-information", dependencies=[Depends(require_internal)])
    async def tool_business_information() -> dict[str, Any]:
        return {"ok": True, "business": settings.service_information}

    @app.post("/internal/tools/check-service-area", dependencies=[Depends(require_internal)])
    async def tool_service_area(payload: RoomflowToolRequest) -> dict[str, Any]:
        business = settings.service_information
        service_area = business.get("service_area", {}) if isinstance(business, dict) else {}
        zip_code = str(payload.data.get("zip") or "").strip()
        allowed_zips = (
            {str(value) for value in service_area.get("zip_codes", [])}
            if isinstance(service_area, dict)
            else set()
        )
        excluded_zips = (
            {str(value) for value in service_area.get("excluded_zip_codes", [])}
            if isinstance(service_area, dict)
            else set()
        )
        if zip_code and zip_code in excluded_zips:
            eligible: bool | None = False
        elif zip_code and allowed_zips:
            eligible = zip_code in allowed_zips
        else:
            eligible = None
        return {
            "ok": True,
            "eligible": eligible,
            "requires_manual_confirmation": eligible is None,
            "service_area": service_area,
        }

    # Public upload portal ---------------------------------------------
    @app.get("/upload/{token}", response_class=HTMLResponse, include_in_schema=False)
    async def upload_form(request: Request, token: str) -> HTMLResponse:
        try:
            request.app.state.business.tokens.verify(token)
        except ValueError:
            return HTMLResponse(
                _upload_page("Link unavailable", "This upload link is invalid or has expired."),
                status_code=410,
            )
        return HTMLResponse(
            _upload_page(
                "Send property photos",
                "Upload clear photos or PDFs for the Floodman team. Do not include payment-card, bank-account, or government-ID information.",
                token=token,
            )
        )

    @app.post("/upload/{token}", response_class=HTMLResponse, include_in_schema=False)
    async def upload_files(
        request: Request,
        token: str,
        files: list[UploadFile] = File(...),
        note: str = Form(default=""),
    ) -> HTMLResponse:
        try:
            token_data = request.app.state.business.tokens.verify(token)
        except ValueError:
            return HTMLResponse(
                _upload_page("Link unavailable", "This upload link is invalid or has expired."),
                status_code=410,
            )
        allowed = set(
            settings.upload_config.get(
                "allowed_content_types",
                ["image/jpeg", "image/png", "image/webp", "image/heic", "application/pdf"],
            )
        )
        max_files = int(settings.upload_config.get("max_files_per_request", 12))
        if not files or len(files) > max_files:
            raise HTTPException(status_code=400, detail=f"Upload between 1 and {max_files} files")
        stored: list[dict[str, Any]] = []
        upload_root = settings.data_dir / "uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        for item in files:
            content_type = str(item.content_type or "application/octet-stream").lower()
            if content_type not in allowed:
                raise HTTPException(status_code=415, detail=f"Unsupported file type: {content_type}")
            original = _safe_filename(item.filename or "upload.bin")
            suffix = Path(original).suffix.lower()[:12]
            stored_name = f"{uuid.uuid4().hex}{suffix}"
            destination = upload_root / stored_name
            total = 0
            header = bytearray()
            with destination.open("wb") as handle:
                while chunk := await item.read(1024 * 1024):
                    if len(header) < 32:
                        header.extend(chunk[: 32 - len(header)])
                    total += len(chunk)
                    if total > settings.max_upload_bytes:
                        handle.close()
                        destination.unlink(missing_ok=True)
                        raise HTTPException(status_code=413, detail="File exceeds upload size limit")
                    handle.write(chunk)
            require_signature = bool(settings.upload_config.get("require_magic_match", True))
            if require_signature and not _matches_upload_signature(content_type, bytes(header)):
                destination.unlink(missing_ok=True)
                raise HTTPException(status_code=415, detail="File contents do not match the declared type")
            destination.chmod(0o600)
            record = request.app.state.database.record_upload(
                {
                    "token_id": token_data.get("tid", ""),
                    "customer_id": token_data.get("customer_id", ""),
                    "call_id": token_data.get("call_id", ""),
                    "filename": original,
                    "stored_path": str(destination),
                    "content_type": content_type,
                    "size_bytes": total,
                    "metadata": {"note": note[:2000]},
                }
            )
            stored.append(record)
        sync = await request.app.state.roomflow.execute(
            "record_upload",
            {
                "customer_id": token_data.get("customer_id", ""),
                "call_id": token_data.get("call_id", ""),
                "uploads": [
                    {
                        "id": value["id"],
                        "filename": value["filename"],
                        "content_type": value["content_type"],
                        "size_bytes": value["size_bytes"],
                    }
                    for value in stored
                ],
                "note": note[:2000],
            },
            idempotency_key=(
                f"upload:{token_data.get('tid','')}:"
                + hashlib.sha256(
                    "|".join(sorted(str(value["id"]) for value in stored)).encode()
                ).hexdigest()[:24]
            ),
        )
        return HTMLResponse(
            _upload_page(
                "Upload complete",
                f"Floodman received {len(stored)} file{'s' if len(stored) != 1 else ''}. You may close this page.",
                success=True,
            )
        )

    # Serve bundled admin UI last so API routes win.
    if settings.web_dir.exists():
        app.mount("/assets", StaticFiles(directory=settings.web_dir), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(settings.web_dir / "index.html")

    @app.exception_handler(Exception)
    async def unhandled_exception(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, HTTPException):
            return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
        logger.exception("Unhandled API error on %s", request.url.path)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()


def run() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        "app.main:app",
        host=settings.web_host,
        port=settings.web_port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips=settings.trusted_proxy_ips,
    )


if __name__ == "__main__":
    run()
