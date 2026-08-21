from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class TranscriptMessage:
    role: str
    text: str
    created_at: str = ""


@dataclass(slots=True)
class IntakeState:
    call_uuid: str
    caller_number: str = ""
    called_number: str = ""
    stage: str = "issue"
    description: str = ""
    service_key: str = ""
    service_status: str = "unknown"
    service_area_status: str = "unknown"
    service_area_city: str = ""
    property_context: str = ""
    timing_summary: str = ""
    safety_summary: str = ""
    name: str = ""
    email: str = ""
    email_status: str = ""
    phone: str = ""
    address: str = ""
    urgency: str = "normal"
    department: str = "estimating"
    completed: bool = False
    unsupported_notice_spoken: bool = False
    confirmations: dict[str, str] = field(default_factory=dict)
    rejected_fields: set[str] = field(default_factory=set)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["rejected_fields"] = sorted(self.rejected_fields)
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "IntakeState":
        data = dict(value)
        data["rejected_fields"] = set(data.get("rejected_fields") or [])
        return cls(**data)


@dataclass(slots=True)
class VoiceReply:
    text: str
    end_call: bool = False
    transfer_number: str = ""
    notification_kind: str = ""
    notification_partial: bool = False
