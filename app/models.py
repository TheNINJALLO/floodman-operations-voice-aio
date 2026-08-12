from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CallType(StrEnum):
    DIRECT_CUSTOMER = "direct_customer"
    GOOGLE_FORWARDED_CUSTOMER = "google_forwarded_customer"
    GOOGLE_AUTOMATED_BUSINESS = "google_automated_business"
    SUSPICIOUS_GOOGLE = "suspicious_google"
    UNKNOWN = "unknown"


class GateState(StrEnum):
    REGISTERED = "registered"
    LISTENING = "listening"
    ANNOUNCEMENT = "announcement"
    WAITING_FOR_HUMAN = "waiting_for_human"
    READY = "ready"
    SECURITY_BLOCK = "security_block"
    TIMEOUT = "timeout"
    FAILED = "failed"


class OutboundPurpose(StrEnum):
    REQUESTED_CALLBACK = "requested_callback"
    MISSED_CALL_CALLBACK = "missed_call_callback"
    BILLING_REMINDER = "billing_reminder"
    PAYMENT_FOLLOWUP = "payment_followup"
    ESTIMATE_FOLLOWUP = "estimate_followup"
    CANCELED_INSPECTION = "canceled_inspection"
    WINBACK = "winback"
    MAINTENANCE = "maintenance"


class JobStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    DIALING = "dialing"
    ANSWERED = "answered"
    COMPLETED = "completed"
    RETRY = "retry"
    FAILED = "failed"
    CANCELED = "canceled"


class GateRegistration(BaseModel):
    gate_uuid: str | None = None
    call_id: str
    caller_number: str = ""
    caller_name: str = ""
    did: str = ""
    trunk: str = ""
    source_hint: str = ""
    direction: str = "inbound"
    metadata: dict[str, Any] = Field(default_factory=dict)


class GateClassificationRequest(BaseModel):
    transcript: str = ""
    source_hint: str = ""
    did: str = ""
    caller_number: str = ""
    elapsed_seconds: float = 0.0
    timed_out: bool = False


class GateDecision(BaseModel):
    call_type: CallType
    state: GateState
    confidence: float = Field(ge=0.0, le=1.0)
    agent: str
    provider: str
    transcript: str = ""
    opening_transcript: str = ""
    announcement_detected: bool = False
    ready: bool = False
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConsentSnapshot(BaseModel):
    customer_id: str = ""
    phone: str
    transactional_voice: bool = False
    marketing_voice_written: bool = False
    sms: bool = False
    email: bool = False
    source: str = ""
    text_version: str = ""
    consent_text: str = ""
    consented_at: datetime | None = None
    revoked_at: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return normalize_phone_value(value)


class EligibilityRequest(BaseModel):
    phone: str
    purpose: OutboundPurpose
    customer_id: str = ""
    timezone: str = "America/Detroit"
    scheduled_for: datetime = Field(default_factory=utc_now)
    requested_at: datetime | None = None
    consent: ConsentSnapshot | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EligibilityDecision(BaseModel):
    allowed: bool
    reason: str
    retry_at: datetime | None = None
    consent_type: str = ""
    policy: dict[str, Any] = Field(default_factory=dict)


class OutboundJobCreate(BaseModel):
    phone: str
    purpose: OutboundPurpose
    customer_id: str = ""
    campaign_id: str = ""
    timezone: str = "America/Detroit"
    scheduled_for: datetime = Field(default_factory=utc_now)
    requested_at: datetime | None = None
    max_attempts: int = Field(default=3, ge=1, le=10)
    agent: str = ""
    consent: ConsentSnapshot | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return normalize_phone_value(value)


class CampaignCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    purpose: OutboundPurpose
    agent: str = ""
    status: str = "draft"
    config: dict[str, Any] = Field(default_factory=dict)


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    status: str | None = None
    agent: str | None = None
    config: dict[str, Any] | None = None


class CampaignAudienceEntry(BaseModel):
    phone: str
    customer_id: str = ""
    timezone: str = "America/Detroit"
    scheduled_for: datetime | None = None
    requested_at: datetime | None = None
    consent: ConsentSnapshot | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return normalize_phone_value(value)


class CampaignEnqueueRequest(BaseModel):
    entries: list[CampaignAudienceEntry] = Field(min_length=1, max_length=1000)
    start_at: datetime = Field(default_factory=utc_now)
    spacing_seconds: int = Field(default=20, ge=0, le=86400)
    max_attempts: int = Field(default=3, ge=1, le=10)
    common_payload: dict[str, Any] = Field(default_factory=dict)


class SuppressionCreate(BaseModel):
    phone: str
    reason: str = "customer_request"
    categories: list[str] = Field(default_factory=lambda: ["all"])
    source: str = "voice"

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return normalize_phone_value(value)


class RoomflowToolRequest(BaseModel):
    call_id: str = ""
    customer_id: str = ""
    caller_number: str = ""
    idempotency_key: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class CallCompletedEvent(BaseModel):
    call_id: str
    caller_number: str = ""
    direction: str = "inbound"
    agent: str = ""
    provider: str = ""
    campaign_id: str = ""
    lead_id: str = ""
    conversation_id: str = ""
    duration: float = 0.0
    outcome: str = ""
    summary: str = ""
    transcript: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TestCallRequest(BaseModel):
    phone: str
    label: str = "production_audio_test"

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, value: str) -> str:
        return normalize_phone_value(value)


class CustomerUpsert(BaseModel):
    id: str = ""
    external_id: str = ""
    name: str = Field(min_length=1, max_length=160)
    phone: str = ""
    email: str = ""
    source: str = "manual"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("phone")
    @classmethod
    def normalize_optional_phone(cls, value: str) -> str:
        return normalize_phone_value(value) if value else ""


class PropertyUpsert(BaseModel):
    id: str = ""
    external_id: str = ""
    customer_id: str
    address: str = Field(min_length=3, max_length=240)
    city: str = ""
    state: str = "MI"
    zip: str = ""
    notes: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class InvoiceUpsert(BaseModel):
    id: str = ""
    external_id: str = ""
    customer_id: str
    invoice_number: str = ""
    status: str = "open"
    amount_due: float = Field(default=0.0, ge=0)
    currency: str = "USD"
    due_date: datetime | None = None
    payment_url: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class VerificationRequest(BaseModel):
    call_id: str
    customer_id: str
    phone: str = ""
    name: str = ""
    street_number: str = ""
    zip: str = ""

    @field_validator("phone")
    @classmethod
    def normalize_optional_phone(cls, value: str) -> str:
        return normalize_phone_value(value) if value else ""


def normalize_phone_value(value: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isdigit() or ch == "+")
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    if cleaned and not cleaned.startswith("+") and len(cleaned) == 10:
        cleaned = "+1" + cleaned
    return cleaned


# ── Call recording models ─────────────────────────────────────────────────────


class RecordingStatus(StrEnum):
    RECORDING = "recording"
    COMPLETED = "completed"
    FAILED = "failed"
    DELETED = "deleted"
    EXPIRED = "expired"
    HELD = "held"
    PROTECTED_SEGMENT = "protected_segment"


class RecordingDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class RecordingSource(StrEnum):
    DIRECT = "direct"
    GOOGLE_LSA = "google_lsa"
    GOOGLE_BUSINESS = "google_business"
    CALLBACK = "callback"
    BILLING = "billing"
    ESTIMATE = "estimate"
    WINBACK = "winback"
    TRANSFER = "transfer"
    TEST = "test"
    UNKNOWN = "unknown"


class RecordingHoldReason(StrEnum):
    LEGAL = "legal"
    OPERATIONAL = "operational"
    COMPLIANCE = "compliance"


class RecordingCreate(BaseModel):
    asterisk_unique_id: str
    call_id: str = ""
    direction: RecordingDirection = RecordingDirection.INBOUND
    caller_number: str = ""
    called_number: str = ""
    agent: str = ""
    campaign_id: str = ""
    source: RecordingSource = RecordingSource.UNKNOWN
    disclosure_played: bool = False
    disclosure_skipped_reason: str = ""


class RecordingMetadata(BaseModel):
    id: str
    asterisk_unique_id: str
    call_id: str = ""
    direction: str
    caller_number: str = ""
    called_number: str = ""
    agent: str = ""
    campaign_id: str = ""
    source: str = "unknown"
    status: str
    file_path: str = ""
    file_size: int = 0
    mime_type: str = "audio/wav"
    sha256: str = ""
    duration_seconds: float = 0.0
    started_at: datetime | None = None
    ended_at: datetime | None = None
    retention_expires_at: datetime | None = None
    is_held: bool = False
    hold_reason: str = ""
    disclosure_played: bool = False
    disclosure_skipped_reason: str = ""
    protected_segment: bool = False
    roomflow_queued: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RecordingListQuery(BaseModel):
    direction: str | None = None
    source: str | None = None
    agent: str | None = None
    campaign_id: str | None = None
    status: str | None = None
    caller_number: str | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    call_id: str | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class RecordingHoldRequest(BaseModel):
    reason: RecordingHoldReason = RecordingHoldReason.LEGAL
    note: str = ""


class RecordingDeleteRequest(BaseModel):
    confirm: bool = False


class RecordingFinalizeEvent(BaseModel):
    asterisk_unique_id: str
    call_id: str = ""
    file_path: str = ""
    duration_seconds: float = 0.0
    transfer_included: bool = False
    protected_segment: bool = False
    error: str = ""
