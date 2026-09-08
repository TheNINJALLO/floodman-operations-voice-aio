from __future__ import annotations

import os
import ipaddress
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(v.strip() for v in os.getenv(name, default).split(",") if v.strip())


@dataclass(frozen=True, slots=True)
class SipTarget:
    host: str
    port: int
    transport: str | None = None

    @property
    def host_literal(self) -> str:
        return f"[{self.host}]" if ":" in self.host else self.host

    @property
    def authority(self) -> str:
        return f"{self.host_literal}:{self.port}"

    @property
    def contact_uri(self) -> str:
        parameter = f";transport={self.transport}" if self.transport else ""
        return f"sip:{self.authority}{parameter}"


def parse_sip_target(value: str, default_port: int = 5060) -> SipTarget:
    """Parse a SIP host or URI once, without ever appending a duplicate port."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("SIP server is empty")
    if any(character.isspace() for character in raw):
        raise ValueError("SIP server must not contain whitespace")
    if not 1 <= int(default_port) <= 65535:
        raise ValueError("SIP port must be between 1 and 65535")

    scheme = ""
    match = re.match(r"^(sips?):", raw, flags=re.IGNORECASE)
    if match:
        scheme = match.group(1).lower()
        raw = raw[match.end() :]
    elif "://" in raw:
        raise ValueError("SIP server scheme must be sip: or sips:")
    if raw.startswith("//") or any(character in raw for character in "/?#"):
        raise ValueError("SIP server must not contain a path, query, or fragment")
    if "@" in raw:
        raise ValueError("SIP_SERVER must not contain a user part")

    target, separator, parameter_text = raw.partition(";")
    transport: str | None = "tls" if scheme == "sips" else None
    if separator:
        for parameter in parameter_text.split(";"):
            name, equals, parameter_value = parameter.partition("=")
            if name.lower() != "transport" or not equals:
                raise ValueError(f"Unsupported SIP URI parameter: {name or 'empty'}")
            candidate = parameter_value.lower()
            if candidate not in {"udp", "tcp", "tls"}:
                raise ValueError("SIP transport must be udp, tcp, or tls")
            if transport and transport != candidate:
                raise ValueError("sips: URI cannot specify a non-TLS transport")
            transport = candidate

    host = ""
    port = int(default_port)
    if target.startswith("["):
        bracketed = re.fullmatch(r"\[([^]]+)](?::([0-9]+))?", target)
        if not bracketed:
            raise ValueError("Malformed bracketed IPv6 SIP server")
        host = bracketed.group(1)
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise ValueError("Invalid IPv6 SIP server") from exc
        if bracketed.group(2):
            port = int(bracketed.group(2))
    else:
        try:
            address = ipaddress.ip_address(target)
        except ValueError:
            if target.count(":") > 1:
                raise ValueError("Malformed SIP server or duplicate port")
            if ":" in target:
                host, port_text = target.rsplit(":", 1)
                if not port_text.isdigit():
                    raise ValueError("SIP port must be numeric")
                port = int(port_text)
            else:
                host = target
            if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?", host):
                raise ValueError("Invalid SIP hostname")
        else:
            host = str(address)

    if not host:
        raise ValueError("SIP host is empty")
    if not 1 <= port <= 65535:
        raise ValueError("SIP port must be between 1 and 65535")
    return SipTarget(host=host, port=port, transport=transport)


@dataclass(slots=True)
class Settings:
    data_dir: Path
    config_dir: Path
    knowledge_dir: Path
    model_dir: Path
    cache_dir: Path
    runtime_dir: Path
    log_dir: Path

    web_host: str
    web_port: int
    admin_token: str
    internal_token: str
    public_base_url: str
    trusted_hosts: tuple[str, ...]

    database_path: Path
    service_area_path: Path

    audiosocket_host: str
    audiosocket_port: int
    llama_base_url: str
    llama_api_key: str
    llama_model_alias: str
    llama_timeout_seconds: float

    faster_whisper_model: str
    faster_whisper_device: str
    faster_whisper_compute_type: str
    faster_whisper_threads: int

    kokoro_model_path: Path
    kokoro_voices_path: Path
    kokoro_voice: str
    kokoro_speed: float
    tts_cache_enabled: bool

    endpoint_silence_ms: int
    contact_endpoint_silence_ms: int
    minimum_speech_ms: int
    maximum_utterance_seconds: float
    post_tts_guard_ms: int
    vad_energy_threshold: int

    sip_mode: str
    sip_server: str
    sip_port: int
    sip_transport: str
    sip_username: str
    sip_password: str
    sip_from_user: str
    sip_from_domain: str
    sip_match_addresses: tuple[str, ...]
    sip_outbound_proxy: str
    sip_local_net: str
    sip_public_ip: str

    twilio_phone_number: str
    outbound_caller_id_number: str
    live_transfer_number: str
    emergency_transfer_number: str
    billing_transfer_number: str
    estimating_transfer_number: str

    team_sms_enabled: bool
    team_alert_numbers: tuple[str, ...]
    estimating_alert_numbers: tuple[str, ...]
    emergency_alert_numbers: tuple[str, ...]
    billing_alert_numbers: tuple[str, ...]
    support_alert_numbers: tuple[str, ...]
    callback_sla_hours: int

    twilio_account_sid: str
    twilio_auth_token: str
    twilio_api_key: str
    twilio_api_key_secret: str
    twilio_messaging_service_sid: str
    twilio_sms_from_number: str

    log_level: str
    app_name: str = "Floodman Voice Appliance"
    timezone: str = "America/Detroit"

    @classmethod
    def from_env(cls) -> "Settings":
        data = Path(os.getenv("DATA_DIR", "/home/container/data"))
        config = Path(os.getenv("CONFIG_DIR", "/opt/floodman/config"))
        knowledge = Path(os.getenv("KNOWLEDGE_DIR", data / "knowledge"))
        models = Path(os.getenv("MODEL_DIR", data / "models"))
        runtime = Path(os.getenv("RUNTIME_DIR", data / "runtime"))
        cache = Path(os.getenv("CACHE_DIR", data / "cache"))
        logs = Path(os.getenv("LOG_DIR", data / "logs"))
        for path in (data, knowledge, models, runtime, cache, logs):
            path.mkdir(parents=True, exist_ok=True)

        admin = os.getenv("ADMIN_TOKEN", "").strip() or secrets.token_urlsafe(32)
        internal = os.getenv("INTERNAL_TOKEN", "").strip() or secrets.token_urlsafe(32)
        public_base_url = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8002").rstrip("/")
        public_host = urlparse(public_base_url).hostname or ""
        trusted_default = ",".join(value for value in ("localhost", "127.0.0.1", public_host) if value)
        return cls(
            data_dir=data,
            config_dir=config,
            knowledge_dir=knowledge,
            model_dir=models,
            cache_dir=cache,
            runtime_dir=runtime,
            log_dir=logs,
            web_host=os.getenv("WEB_HOST", "0.0.0.0"),
            web_port=_int("WEB_PORT", 8002),
            admin_token=admin,
            internal_token=internal,
            public_base_url=public_base_url,
            trusted_hosts=_csv("TRUSTED_HOSTS", trusted_default),
            database_path=Path(os.getenv("DATABASE_PATH", data / "floodman.db")),
            service_area_path=Path(os.getenv("SERVICE_AREA_PATH", data / "service_area.yaml")),
            audiosocket_host=os.getenv("AUDIOSOCKET_HOST", "127.0.0.1"),
            audiosocket_port=_int("AUDIOSOCKET_PORT", 8090),
            llama_base_url=os.getenv("LLAMA_BASE_URL", "http://127.0.0.1:8081/v1").rstrip("/"),
            llama_api_key=os.getenv("LLAMA_API_KEY", "floodman-local"),
            llama_model_alias=os.getenv("LLAMA_MODEL_ALIAS", "floodman-qwen3-4b"),
            llama_timeout_seconds=_float("LLAMA_TIMEOUT_SECONDS", 8.0),
            faster_whisper_model=os.getenv("FASTER_WHISPER_MODEL", "small.en"),
            faster_whisper_device=os.getenv("FASTER_WHISPER_DEVICE", "cpu"),
            faster_whisper_compute_type=os.getenv("FASTER_WHISPER_COMPUTE_TYPE", "int8"),
            faster_whisper_threads=_int("FASTER_WHISPER_THREADS", max(2, (os.cpu_count() or 4) // 2)),
            kokoro_model_path=Path(os.getenv("KOKORO_MODEL_PATH", models / "kokoro" / "kokoro-v1.0.onnx")),
            kokoro_voices_path=Path(os.getenv("KOKORO_VOICES_PATH", models / "kokoro" / "voices-v1.0.bin")),
            kokoro_voice=os.getenv("KOKORO_VOICE", "af_heart"),
            kokoro_speed=_float("KOKORO_SPEED", 1.02),
            tts_cache_enabled=_bool("TTS_CACHE_ENABLED", True),
            endpoint_silence_ms=_int("ENDPOINT_SILENCE_MS", 550),
            contact_endpoint_silence_ms=_int("CONTACT_ENDPOINT_SILENCE_MS", 1200),
            minimum_speech_ms=_int("MINIMUM_SPEECH_MS", 160),
            maximum_utterance_seconds=_float("MAXIMUM_UTTERANCE_SECONDS", 25.0),
            post_tts_guard_ms=_int("POST_TTS_GUARD_MS", 120),
            vad_energy_threshold=_int("VAD_ENERGY_THRESHOLD", 325),
            sip_mode=os.getenv("SIP_MODE", "twilio").strip().lower(),
            sip_server=os.getenv("SIP_SERVER", "").strip(),
            sip_port=_int("SIP_PORT", 5060),
            sip_transport=os.getenv("SIP_TRANSPORT", "udp").strip().lower(),
            sip_username=os.getenv("SIP_USERNAME", "").strip(),
            sip_password=os.getenv("SIP_PASSWORD", "").strip(),
            sip_from_user=os.getenv("SIP_FROM_USER", "").strip(),
            sip_from_domain=os.getenv("SIP_FROM_DOMAIN", "").strip(),
            sip_match_addresses=_csv("SIP_MATCH_ADDRESSES", "54.172.60.0/23,54.244.51.0/24,177.71.206.192/26"),
            sip_outbound_proxy=os.getenv("SIP_OUTBOUND_PROXY", "").strip(),
            sip_local_net=os.getenv("SIP_LOCAL_NET", "172.16.0.0/12").strip(),
            sip_public_ip=os.getenv("PUBLIC_IP", "").strip(),
            twilio_phone_number=os.getenv("TWILIO_PHONE_NUMBER", "").strip(),
            outbound_caller_id_number=os.getenv("OUTBOUND_CALLER_ID_NUMBER", "").strip(),
            live_transfer_number=os.getenv("FLOODMAN_LIVE_NUMBER", "").strip(),
            emergency_transfer_number=os.getenv("FLOODMAN_EMERGENCY_NUMBER", "").strip(),
            billing_transfer_number=os.getenv("FLOODMAN_BILLING_NUMBER", "").strip(),
            estimating_transfer_number=os.getenv("FLOODMAN_ESTIMATING_NUMBER", "").strip(),
            team_sms_enabled=_bool("FLOODMAN_TEAM_SMS_ENABLED", True),
            team_alert_numbers=_csv("FLOODMAN_TEAM_ALERT_NUMBERS"),
            estimating_alert_numbers=_csv("FLOODMAN_ESTIMATING_ALERT_NUMBERS"),
            emergency_alert_numbers=_csv("FLOODMAN_EMERGENCY_ALERT_NUMBERS"),
            billing_alert_numbers=_csv("FLOODMAN_BILLING_ALERT_NUMBERS"),
            support_alert_numbers=_csv("FLOODMAN_SUPPORT_ALERT_NUMBERS"),
            callback_sla_hours=max(1, _int("FLOODMAN_CALLBACK_SLA_HOURS", 24)),
            twilio_account_sid=os.getenv("TWILIO_ACCOUNT_SID", "").strip(),
            twilio_auth_token=os.getenv("TWILIO_AUTH_TOKEN", "").strip(),
            twilio_api_key=os.getenv("TWILIO_API_KEY", "").strip(),
            twilio_api_key_secret=os.getenv("TWILIO_API_KEY_SECRET", "").strip(),
            twilio_messaging_service_sid=os.getenv("TWILIO_MESSAGING_SERVICE_SID", "").strip(),
            twilio_sms_from_number=os.getenv("TWILIO_SMS_FROM_NUMBER", "").strip(),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )

    @property
    def llm_model_path(self) -> Path:
        return Path(os.getenv("LLAMA_MODEL_PATH", self.model_dir / "llm" / "Qwen3-4B-Q4_K_M.gguf"))

    @property
    def twilio_sms_configured(self) -> bool:
        auth = bool(self.twilio_account_sid and (self.twilio_auth_token or (self.twilio_api_key and self.twilio_api_key_secret)))
        sender = bool(self.twilio_messaging_service_sid or self.twilio_sms_from_number)
        return self.team_sms_enabled and auth and sender
