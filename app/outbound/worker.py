from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app.compliance.engine import ComplianceEngine
from app.config import Settings
from app.db import Database
from app.models import ConsentSnapshot, EligibilityRequest, JobStatus, OutboundPurpose
from app.notifications import TwilioTeamNotifier
from app.outbound.ami import AMIClient
from app.roomflow.client import RoomflowClient

logger = logging.getLogger(__name__)


class OutboundWorker:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        compliance: ComplianceEngine,
        ami: AMIClient,
        roomflow: RoomflowClient,
    ):
        self.settings = settings
        self.database = database
        self.compliance = compliance
        self.ami = ami
        self.roomflow = roomflow
        self.team_notifier = TwilioTeamNotifier(settings)
        self._stop = asyncio.Event()
        self._semaphore = asyncio.Semaphore(settings.outbound_concurrency)
        self._last_outbox_run = 0.0

    async def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("Outbound worker started")
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            try:
                if self.settings.outbound_enabled and self.settings.ami_enabled:
                    await self.process_due_jobs()
                    await self.recover_stale_jobs()
                now = loop.time()
                if now - self._last_outbox_run >= self.settings.outbox_poll_seconds:
                    await self.process_outbox()
                    self._last_outbox_run = now
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Outbound worker loop failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.worker_poll_seconds)
            except asyncio.TimeoutError:
                pass
        logger.info("Outbound worker stopped")

    async def process_due_jobs(self) -> None:
        jobs = self.database.due_outbound_jobs(limit=max(10, self.settings.outbound_concurrency * 3))
        if not jobs:
            return
        await asyncio.gather(*(self._guarded_process(job) for job in jobs))

    async def _guarded_process(self, job: dict) -> None:
        async with self._semaphore:
            current = self.database.get_outbound_job(str(job["id"]))
            if not current or current.get("status") not in {JobStatus.PENDING.value, JobStatus.RETRY.value}:
                return
            await self._process_job(current)

    async def _process_job(self, job: dict) -> None:
        try:
            purpose = OutboundPurpose(job["purpose"])
            consent = ConsentSnapshot.model_validate(job["consent"]) if job.get("consent") else None
            request = EligibilityRequest(
                phone=job["phone"],
                purpose=purpose,
                customer_id=job.get("customer_id") or "",
                timezone=job.get("timezone") or self.settings.timezone,
                scheduled_for=datetime.now(timezone.utc),
                requested_at=(
                    datetime.fromisoformat(job["requested_at"]) if job.get("requested_at") else None
                ),
                consent=consent,
                metadata=job.get("payload") or {},
            )
            eligibility = self.compliance.evaluate(request)
            if not eligibility.allowed:
                retryable_reasons = {
                    "outside_calling_window",
                    "blackout_date",
                    "frequency_cap",
                    "recent_live_contact",
                }
                if eligibility.retry_at and eligibility.reason in retryable_reasons:
                    self.database.update_job(
                        job["id"],
                        status=JobStatus.RETRY.value,
                        next_attempt_at=eligibility.retry_at,
                        eligibility_reason=eligibility.reason,
                        consent_type=eligibility.consent_type,
                    )
                else:
                    self.database.update_job(
                        job["id"],
                        status=JobStatus.BLOCKED.value,
                        eligibility_reason=eligibility.reason,
                        consent_type=eligibility.consent_type,
                    )
                logger.info("Blocked outbound job %s: %s", job["id"], eligibility.reason)
                return

            self.database.update_job(
                job["id"],
                status=JobStatus.DIALING.value,
                eligibility_reason=eligibility.reason,
                consent_type=eligibility.consent_type,
                last_error=None,
            )
            result = await self.ami.originate(
                phone=job["phone"],
                agent=job["agent"],
                job_id=job["id"],
                purpose=purpose.value,
                customer_id=job.get("customer_id") or "",
                extra_variables={
                    "AAVA_CAMPAIGN_ID": str(job.get("campaign_id") or ""),
                    "FLOODMAN_CAMPAIGN_ID": str(job.get("campaign_id") or ""),
                },
            )
            attempts = int(job.get("attempts") or 0) + 1
            if result.ok:
                status = JobStatus.ANSWERED.value if result.answered else JobStatus.DIALING.value
                self.database.update_job(
                    job["id"],
                    status=status,
                    attempts=attempts,
                    ami_action_id=result.action_id,
                    asterisk_channel=result.channel,
                    answered_at=datetime.now(timezone.utc) if result.answered else None,
                    eligibility_reason=eligibility.reason,
                    consent_type=eligibility.consent_type,
                    next_attempt_at=None,
                    last_error=None,
                )
                self.database.add_call_event(
                    job["id"],
                    "outbound",
                    "originate_answered" if result.answered else "originate_accepted",
                    {
                        "phone": job["phone"],
                        "purpose": purpose.value,
                        "action_id": result.action_id,
                        "channel": result.channel,
                    },
                )
                return
            await self._retry_or_fail(
                job,
                attempts,
                result.message or f"AMI originate failed ({result.reason_code})",
            )
        except Exception as exc:
            logger.exception("Outbound job %s failed", job.get("id"))
            attempts = int(job.get("attempts") or 0) + 1
            await self._retry_or_fail(job, attempts, str(exc))

    async def _retry_or_fail(self, job: dict, attempts: int, error: str) -> None:
        if attempts >= int(job.get("max_attempts") or 3):
            self.database.update_job(
                job["id"],
                status=JobStatus.FAILED.value,
                attempts=attempts,
                last_error=error[:1000],
                next_attempt_at=None,
            )
            return
        delay_minutes = min(240, 5 * (2 ** max(0, attempts - 1)))
        self.database.update_job(
            job["id"],
            status=JobStatus.RETRY.value,
            attempts=attempts,
            last_error=error[:1000],
            next_attempt_at=datetime.now(timezone.utc) + timedelta(minutes=delay_minutes),
        )

    async def recover_stale_jobs(self) -> None:
        # Once AMI accepts an originate, an automatic redial can duplicate a real
        # customer call when only the completion webhook was lost. Fail closed into
        # manual review instead of retrying an uncertain live-contact outcome.
        for job in self.database.stale_dialing_jobs(self.settings.dialing_timeout_seconds):
            self.database.update_job(
                job["id"],
                status=JobStatus.FAILED.value,
                last_error="completion_status_unknown_manual_review",
                next_attempt_at=None,
            )
            self.database.add_call_event(
                str(job["id"]),
                "outbound",
                "completion_unknown_manual_review",
                {
                    "phone": job.get("phone") or "",
                    "purpose": job.get("purpose") or "",
                    "previous_status": job.get("status") or "",
                },
            )

    async def process_outbox(self) -> None:
        for item in self.database.due_outbox(limit=10):
            operation = str(item.get("operation") or "")
            if operation == "team_sms_alert":
                result = await self.team_notifier.send(dict(item.get("payload") or {}))
                self.database.mark_outbox(
                    str(item["id"]),
                    bool(result.get("ok")),
                    str(result.get("error") or ""),
                )
                continue
            if not self.settings.roomflow_enabled:
                continue
            result = await self.roomflow.replay_outbox_item(item)
            self.database.mark_outbox(item["id"], result.ok, result.error)
