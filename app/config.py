from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return value


@dataclass(slots=True)
class Settings:
    project_root: Path
    data_dir: Path
    config_dir: Path
    web_dir: Path
    knowledge_dir: Path

    app_name: str = "Floodman Operations Voice AIO"
    environment: str = "production"
    web_host: str = "0.0.0.0"
    web_port: int = 9000
    admin_token: str = ""
    internal_token: str = ""
    upload_token_secret: str = ""
    public_base_url: str = "http://127.0.0.1:9000"
    trusted_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    trusted_proxy_ips: str = "127.0.0.1"
    force_https: bool = False
    enable_api_docs: bool = False
    print_bootstrap_secrets: bool = False
    security_headers_enabled: bool = True

    database_path: Path = Path("/home/container/data/floodman-voice.sqlite3")

    gate_enabled: bool = True
    gate_host: str = "0.0.0.0"
    gate_port: int = 9019
    gate_min_seconds: float = 1.0
    gate_max_seconds: float = 11.0
    gate_transcribe_interval_seconds: float = 1.8
    gate_no_speech_timeout_seconds: float = 3.5
    gate_transcriber: str = "faster-whisper"
    gate_stt_url: str = ""
    gate_stt_api_key: str = ""
    gate_stt_model: str = "tiny.en"
    gate_stt_language: str = "en"
    faster_whisper_model: str = "tiny.en"
    faster_whisper_device: str = "cpu"
    faster_whisper_compute_type: str = "int8"

    default_agent: str = "floodman_inbound"
    default_provider: str = "local_hybrid"
    google_forwarded_agent: str = "floodman_inbound"
    google_business_agent: str = "floodman_google_business"
    suspicious_google_agent: str = "floodman_security"
    billing_agent: str = "floodman_billing"
    callback_agent: str = "floodman_callback"
    estimate_followup_agent: str = "floodman_estimate_followup"
    winback_agent: str = "floodman_winback"

    local_crm_enabled: bool = True
    roomflow_enabled: bool = False
    roomflow_base_url: str = ""
    roomflow_token: str = ""
    roomflow_company_id: str = ""
    roomflow_timeout_seconds: float = 8.0
    roomflow_verify_tls: bool = True
    roomflow_sync_local_writes: bool = True

    # Internal team SMS notifications for completed customer intake.
    team_sms_enabled: bool = False
    team_alert_numbers: tuple[str, ...] = ()
    estimating_alert_numbers: tuple[str, ...] = ()
    emergency_alert_numbers: tuple[str, ...] = ()
    billing_alert_numbers: tuple[str, ...] = ()
    support_alert_numbers: tuple[str, ...] = ()
    callback_sla_hours: int = 24
    team_sms_timeout_seconds: float = 8.0
    twilio_account_sid: str = ""
    twilio_api_key: str = ""
    twilio_api_key_secret: str = ""
    twilio_auth_token: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_sms_from_number: str = ""

    ami_enabled: bool = False
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_username: str = "floodman"
    ami_secret: str = ""
    ami_timeout_seconds: float = 55.0
    asterisk_trunk: str = "floodman-trunk"
    outbound_caller_id_name: str = "Floodman"
    outbound_caller_id_number: str = ""
    ava_stasis_app: str = "asterisk-ai-voice-agent"
    ava_enabled: bool = True
    asterisk_mode: str = "embedded"
    asterisk_host: str = "127.0.0.1"
    asterisk_ari_scheme: str = "http"
    asterisk_ari_port: int = 8088
    asterisk_ari_username: str = "floodman-ava"
    asterisk_ari_password: str = ""
    sip_trunk_mode: str = "disabled"
    test_calls_enabled: bool = False
    test_call_allowlist: tuple[str, ...] = ()

    outbound_enabled: bool = False
    worker_poll_seconds: float = 2.0
    outbox_poll_seconds: float = 10.0
    dialing_timeout_seconds: int = 7200
    outbound_concurrency: int = 3

    agents_db_path: Path = Path("/home/container/data/ava/operator/agents.db")
    reconcile_ava_agents: bool = True
    ava_provider: str = "local_hybrid"
    ava_pipeline: str = "local_hybrid"
    ava_audio_profile: str = "telephony_enhanced_8k"

    # ── Call recording ────────────────────────────────────────────────────────
    call_recording_enabled: bool = False
    call_recording_format: str = "wav"
    call_recording_beep_enabled: bool = False
    call_recording_disclosure_enabled: bool = True
    call_recording_disclosure_message: str = (
        "This call may be recorded for service and quality purposes."
    )
    call_recording_retention_days: int = 90
    call_recording_include_transfers: bool = True
    call_recording_storage_dir: Path = Path("/home/container/data/recordings")

    timezone: str = "America/Detroit"
    log_level: str = "INFO"
    max_upload_bytes: int = 25 * 1024 * 1024

    knowledge_require_approved: bool = True
    knowledge_top_k: int = 4
    knowledge_max_chars: int = 5200
    knowledge_min_score: float = 0.8

    config: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "Settings":
        root = project_root or Path(
            os.getenv("FLOODMAN_PROJECT_ROOT", Path(__file__).resolve().parents[1])
        )
        data_dir = Path(os.getenv("DATA_DIR", "/home/container/data"))
        if not data_dir.is_absolute():
            data_dir = root / data_dir
        config_dir = Path(os.getenv("CONFIG_DIR", root / "config"))
        web_dir = Path(os.getenv("WEB_DIR", root / "web"))
        knowledge_dir = Path(os.getenv("KNOWLEDGE_DIR", data_dir / "knowledge"))
        if not knowledge_dir.is_absolute():
            knowledge_dir = data_dir / knowledge_dir
        config = _load_yaml(config_dir / "floodman.yaml")

        admin_token = os.getenv("ADMIN_TOKEN", "").strip() or secrets.token_urlsafe(32)
        internal_token = os.getenv("INTERNAL_TOKEN", "").strip() or secrets.token_urlsafe(32)
        upload_token_secret = (
            os.getenv("UPLOAD_TOKEN_SECRET", "").strip() or secrets.token_urlsafe(48)
        )

        settings = cls(
            project_root=root,
            data_dir=data_dir,
            config_dir=config_dir,
            web_dir=web_dir,
            knowledge_dir=knowledge_dir,
            app_name=os.getenv("APP_NAME", "Floodman Operations Voice AIO"),
            environment=os.getenv("ENVIRONMENT", "production"),
            web_host=os.getenv("WEB_HOST", "0.0.0.0"),
            web_port=_env_int("WEB_PORT", 9000),
            admin_token=admin_token,
            internal_token=internal_token,
            upload_token_secret=upload_token_secret,
            public_base_url=os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:9000").rstrip("/"),
            trusted_hosts=_env_csv("TRUSTED_HOSTS", "localhost,127.0.0.1,testserver"),
            trusted_proxy_ips=os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1").strip(),
            force_https=_env_bool("FORCE_HTTPS", False),
            enable_api_docs=_env_bool("ENABLE_API_DOCS", False),
            print_bootstrap_secrets=_env_bool("PRINT_BOOTSTRAP_SECRETS", False),
            security_headers_enabled=_env_bool("SECURITY_HEADERS_ENABLED", True),
            database_path=Path(
                os.getenv("DATABASE_PATH", data_dir / "floodman-voice.sqlite3")
            ),
            gate_enabled=_env_bool("GATE_ENABLED", True),
            gate_host=os.getenv("GATE_HOST", "0.0.0.0"),
            gate_port=_env_int("GATE_PORT", 9019),
            gate_min_seconds=_env_float("GATE_MIN_SECONDS", 1.0),
            gate_max_seconds=_env_float("GATE_MAX_SECONDS", 11.0),
            gate_transcribe_interval_seconds=_env_float(
                "GATE_TRANSCRIBE_INTERVAL_SECONDS", 1.8
            ),
            gate_no_speech_timeout_seconds=max(
                1.0, min(8.0, _env_float("GATE_NO_SPEECH_TIMEOUT_SECONDS", 3.5))
            ),
            gate_transcriber=os.getenv("GATE_TRANSCRIBER", "faster-whisper").strip().lower(),
            gate_stt_url=os.getenv("GATE_STT_URL", "").strip(),
            gate_stt_api_key=os.getenv("GATE_STT_API_KEY", "").strip(),
            gate_stt_model=os.getenv("GATE_STT_MODEL", "tiny.en"),
            gate_stt_language=os.getenv("GATE_STT_LANGUAGE", "en"),
            faster_whisper_model=os.getenv("FASTER_WHISPER_MODEL", "tiny.en"),
            faster_whisper_device=os.getenv("FASTER_WHISPER_DEVICE", "cpu"),
            faster_whisper_compute_type=os.getenv("FASTER_WHISPER_COMPUTE_TYPE", "int8"),
            default_agent=os.getenv("DEFAULT_AGENT", "floodman_inbound"),
            default_provider=os.getenv("DEFAULT_PROVIDER", "local_hybrid"),
            google_forwarded_agent=os.getenv("GOOGLE_FORWARDED_AGENT", "floodman_inbound"),
            google_business_agent=os.getenv(
                "GOOGLE_BUSINESS_AGENT", "floodman_google_business"
            ),
            suspicious_google_agent=os.getenv(
                "SUSPICIOUS_GOOGLE_AGENT", "floodman_security"
            ),
            billing_agent=os.getenv("BILLING_AGENT", "floodman_billing"),
            callback_agent=os.getenv("CALLBACK_AGENT", "floodman_callback"),
            estimate_followup_agent=os.getenv(
                "ESTIMATE_FOLLOWUP_AGENT", "floodman_estimate_followup"
            ),
            winback_agent=os.getenv("WINBACK_AGENT", "floodman_winback"),
            local_crm_enabled=_env_bool("LOCAL_CRM_ENABLED", True),
            roomflow_enabled=_env_bool("ROOMFLOW_ENABLED", False),
            roomflow_base_url=os.getenv("ROOMFLOW_BASE_URL", "").rstrip("/"),
            roomflow_token=os.getenv("ROOMFLOW_TOKEN", ""),
            roomflow_company_id=os.getenv("ROOMFLOW_COMPANY_ID", ""),
            roomflow_timeout_seconds=_env_float("ROOMFLOW_TIMEOUT_SECONDS", 8.0),
            roomflow_verify_tls=_env_bool("ROOMFLOW_VERIFY_TLS", True),
            roomflow_sync_local_writes=_env_bool("ROOMFLOW_SYNC_LOCAL_WRITES", True),
            team_sms_enabled=_env_bool("FLOODMAN_TEAM_SMS_ENABLED", False),
            team_alert_numbers=_env_csv("FLOODMAN_TEAM_ALERT_NUMBERS", ""),
            estimating_alert_numbers=_env_csv("FLOODMAN_ESTIMATING_ALERT_NUMBERS", ""),
            emergency_alert_numbers=_env_csv("FLOODMAN_EMERGENCY_ALERT_NUMBERS", ""),
            billing_alert_numbers=_env_csv("FLOODMAN_BILLING_ALERT_NUMBERS", ""),
            support_alert_numbers=_env_csv("FLOODMAN_SUPPORT_ALERT_NUMBERS", ""),
            callback_sla_hours=max(1, _env_int("FLOODMAN_CALLBACK_SLA_HOURS", 24)),
            team_sms_timeout_seconds=max(2.0, _env_float("FLOODMAN_TEAM_SMS_TIMEOUT_SECONDS", 8.0)),
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            twilio_api_key=os.getenv("TWILIO_API_KEY", "").strip(),
            twilio_api_key_secret=os.getenv("TWILIO_API_KEY_SECRET", "").strip(),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            twilio_messaging_service_sid=os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip(),
            twilio_sms_from_number=os.getenv(
                "TWILIO_SMS_FROM_NUMBER",
                os.getenv("TWILIO_FROM_NUMBER", os.getenv("TWILIO_PHONE_NUMBER", "")),
            ).strip(),
            ami_enabled=_env_bool("AMI_ENABLED", False),
            ami_host=os.getenv("AMI_HOST", "127.0.0.1"),
            ami_port=_env_int("AMI_PORT", 5038),
            ami_username=os.getenv("AMI_USERNAME", "floodman"),
            ami_secret=os.getenv("AMI_SECRET", ""),
            ami_timeout_seconds=_env_float("AMI_TIMEOUT_SECONDS", 55.0),
            asterisk_trunk=os.getenv("ASTERISK_TRUNK", "floodman-trunk"),
            outbound_caller_id_name=os.getenv("OUTBOUND_CALLER_ID_NAME", "Floodman"),
            outbound_caller_id_number=os.getenv("OUTBOUND_CALLER_ID_NUMBER", ""),
            ava_stasis_app=os.getenv("AVA_STASIS_APP", "asterisk-ai-voice-agent"),
            ava_enabled=_env_bool("AVA_ENABLED", True),
            asterisk_mode=os.getenv("ASTERISK_MODE", "embedded").strip().lower(),
            asterisk_host=os.getenv("ASTERISK_HOST", "127.0.0.1"),
            asterisk_ari_scheme=os.getenv("ASTERISK_ARI_SCHEME", "http").strip().lower(),
            asterisk_ari_port=_env_int("ASTERISK_ARI_PORT", _env_int("ARI_PORT", 8088)),
            asterisk_ari_username=os.getenv(
                "ASTERISK_ARI_USERNAME", os.getenv("ARI_USERNAME", "floodman-ava")
            ),
            asterisk_ari_password=os.getenv(
                "ASTERISK_ARI_PASSWORD", os.getenv("ARI_SECRET", "")
            ),
            sip_trunk_mode=os.getenv("SIP_TRUNK_MODE", "disabled").strip().lower(),
            test_calls_enabled=_env_bool("TEST_CALLS_ENABLED", False),
            test_call_allowlist=_env_csv("TEST_CALL_ALLOWLIST", ""),
            outbound_enabled=_env_bool("OUTBOUND_ENABLED", False),
            worker_poll_seconds=_env_float("WORKER_POLL_SECONDS", 2.0),
            outbox_poll_seconds=_env_float("OUTBOX_POLL_SECONDS", 10.0),
            dialing_timeout_seconds=_env_int("DIALING_TIMEOUT_SECONDS", 7200),
            outbound_concurrency=max(1, _env_int("OUTBOUND_CONCURRENCY", 3)),
            agents_db_path=Path(
                os.getenv("AGENTS_DB_PATH", data_dir / "ava/operator/agents.db")
            ),
            reconcile_ava_agents=_env_bool("RECONCILE_AVA_AGENTS", True),
            ava_provider=os.getenv(
                "AVA_PROVIDER", os.getenv("DEFAULT_PROVIDER", "local_hybrid")
            ),
            ava_pipeline=os.getenv("AVA_PIPELINE", "local_hybrid"),
            ava_audio_profile=os.getenv("AVA_AUDIO_PROFILE", "telephony_enhanced_8k"),
            call_recording_enabled=_env_bool("CALL_RECORDING_ENABLED", False),
            call_recording_format=os.getenv("CALL_RECORDING_FORMAT", "wav").strip().lower(),
            call_recording_beep_enabled=_env_bool("CALL_RECORDING_BEEP_ENABLED", False),
            call_recording_disclosure_enabled=_env_bool(
                "CALL_RECORDING_DISCLOSURE_ENABLED", True
            ),
            call_recording_disclosure_message=os.getenv(
                "CALL_RECORDING_DISCLOSURE_MESSAGE",
                "This call may be recorded for service and quality purposes.",
            ).strip(),
            call_recording_retention_days=max(
                1, _env_int("CALL_RECORDING_RETENTION_DAYS", 90)
            ),
            call_recording_include_transfers=_env_bool(
                "CALL_RECORDING_INCLUDE_TRANSFERS", True
            ),
            call_recording_storage_dir=Path(
                os.getenv(
                    "CALL_RECORDING_STORAGE_DIR",
                    data_dir / "recordings",
                )
            ),
            timezone=os.getenv("DEFAULT_TIMEZONE", "America/Detroit"),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 25 * 1024 * 1024),
            knowledge_require_approved=_env_bool("KNOWLEDGE_REQUIRE_APPROVED", True),
            knowledge_top_k=max(1, min(8, _env_int("KNOWLEDGE_TOP_K", 4))),
            knowledge_max_chars=max(800, min(12000, _env_int("KNOWLEDGE_MAX_CHARS", 5200))),
            knowledge_min_score=max(0.0, _env_float("KNOWLEDGE_MIN_SCORE", 0.8)),
            config=config,
        )
        settings.ensure_directories()
        return settings

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.agents_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.call_recording_storage_dir.mkdir(parents=True, exist_ok=True)
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        for name in ("gate-audio", "logs", "uploads", "recordings", "asterisk"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)

    @property
    def roomflow_endpoints(self) -> dict[str, Any]:
        endpoints = self.config.get("roomflow", {}).get("endpoints", {})
        return endpoints if isinstance(endpoints, dict) else {}

    @property
    def compliance_config(self) -> dict[str, Any]:
        value = self.config.get("compliance", {})
        return value if isinstance(value, dict) else {}

    @property
    def transfer_destinations(self) -> dict[str, Any]:
        value = self.config.get("transfer_destinations", {})
        return value if isinstance(value, dict) else {}

    @property
    def service_information(self) -> dict[str, Any]:
        value = self.config.get("business", {})
        return value if isinstance(value, dict) else {}

    @property
    def scheduling_config(self) -> dict[str, Any]:
        value = self.config.get("scheduling", {})
        return value if isinstance(value, dict) else {}

    @property
    def upload_config(self) -> dict[str, Any]:
        value = self.config.get("uploads", {})
        return value if isinstance(value, dict) else {}
