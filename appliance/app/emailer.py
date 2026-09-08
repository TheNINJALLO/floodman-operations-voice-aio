from __future__ import annotations

import asyncio
import json
import logging
import re
import smtplib
import ssl
from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
SMTP_SECURITY_MODES = {"starttls", "tls", "none"}


def normalize_user_email(value: Any) -> str:
    email = str(value or "").strip().lower()
    if not email:
        return ""
    if len(email) > 320 or not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Enter a valid email address.")
    return email


def _safe_header(value: Any, maximum: int) -> str:
    return re.sub(r"[\r\n]+", " ", str(value or "")).strip()[:maximum]


@dataclass(slots=True)
class EmailConfiguration:
    enabled: bool = False
    host: str = ""
    port: int = 587
    security: str = "starttls"
    username: str = ""
    password: str = ""
    from_email: str = ""
    from_name: str = "Floodman Call Center"
    timeout_seconds: float = 10.0

    @property
    def configured(self) -> bool:
        credentials_complete = bool(self.username) == bool(self.password)
        return bool(
            self.enabled
            and self.host
            and self.from_email
            and credentials_complete
            and self.security in SMTP_SECURITY_MODES
        )

    def public_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "host": self.host,
            "port": self.port,
            "security": self.security,
            "username": self.username,
            "password_configured": bool(self.password),
            "from_email": self.from_email,
            "from_name": self.from_name,
            "timeout_seconds": self.timeout_seconds,
        }


class EmailService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.email_settings_path
        self._configuration = self._load()

    @property
    def configuration(self) -> EmailConfiguration:
        return self._configuration

    def _environment_configuration(self) -> EmailConfiguration:
        return self._validate(
            {
                "enabled": self.settings.email_enabled,
                "host": self.settings.smtp_host,
                "port": self.settings.smtp_port,
                "security": self.settings.smtp_security,
                "username": self.settings.smtp_username,
                "password": self.settings.smtp_password,
                "from_email": self.settings.smtp_from_email,
                "from_name": self.settings.smtp_from_name,
                "timeout_seconds": self.settings.smtp_timeout_seconds,
            }
        )

    def _load(self) -> EmailConfiguration:
        if not self.path.exists():
            return self._environment_configuration()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("Email settings must be a JSON object")
            return self._validate(value)
        except (OSError, TypeError, ValueError) as exc:
            logger.error("Persisted email configuration is invalid; email delivery disabled: %s", type(exc).__name__)
            return EmailConfiguration()

    @staticmethod
    def _validate(values: dict[str, Any]) -> EmailConfiguration:
        host = str(values.get("host") or "").strip()
        if host and (len(host) > 255 or not HOST_PATTERN.fullmatch(host)):
            raise ValueError("SMTP host must be a valid hostname or IP address.")
        try:
            port = int(values.get("port", 587))
        except (TypeError, ValueError) as exc:
            raise ValueError("SMTP port must be a number.") from exc
        if not 1 <= port <= 65535:
            raise ValueError("SMTP port must be between 1 and 65535.")
        security = str(values.get("security") or "starttls").strip().lower()
        if security not in SMTP_SECURITY_MODES:
            raise ValueError("SMTP security must be STARTTLS, TLS, or none.")
        username = _safe_header(values.get("username"), 320)
        password = str(values.get("password") or "")[:1024]
        if bool(username) != bool(password):
            raise ValueError("SMTP username and password must either both be supplied or both be blank.")
        if security == "none" and username:
            raise ValueError("SMTP credentials require STARTTLS or TLS security.")
        from_email = normalize_user_email(values.get("from_email"))
        from_name = _safe_header(values.get("from_name"), 120) or "Floodman Call Center"
        try:
            timeout_seconds = float(values.get("timeout_seconds", 10.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("SMTP timeout must be a number.") from exc
        if not 3.0 <= timeout_seconds <= 30.0:
            raise ValueError("SMTP timeout must be between 3 and 30 seconds.")
        enabled = bool(values.get("enabled"))
        if enabled and (not host or not from_email):
            raise ValueError("SMTP host and sender email are required when email delivery is enabled.")
        return EmailConfiguration(
            enabled=enabled,
            host=host,
            port=port,
            security=security,
            username=username,
            password=password,
            from_email=from_email,
            from_name=from_name,
            timeout_seconds=timeout_seconds,
        )

    def configure(self, values: dict[str, Any], *, keep_password: bool = True) -> EmailConfiguration:
        candidate = dict(values)
        if not candidate.get("username"):
            candidate["password"] = ""
        elif keep_password and not candidate.get("password"):
            candidate["password"] = self._configuration.password
        configuration = self._validate(candidate)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(asdict(configuration), indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self._configuration = configuration
        return configuration

    async def send(self, recipient: str, subject: str, body: str) -> tuple[str, str]:
        try:
            recipient = normalize_user_email(recipient)
        except ValueError:
            return "failed", "Recipient email address is invalid"
        configuration = self._configuration
        if not recipient:
            return "failed", "Recipient email address is missing"
        if not configuration.configured:
            return "not_configured", "Email delivery is disabled or SMTP settings are incomplete"
        try:
            await asyncio.to_thread(self._send_sync, configuration, recipient, subject, body)
            return "sent", "SMTP server accepted the message"
        except (OSError, TimeoutError, smtplib.SMTPException, ssl.SSLError) as exc:
            logger.warning("Email delivery failed recipient=%s error=%s", recipient, type(exc).__name__)
            return "failed", f"SMTP delivery failed: {type(exc).__name__}"

    @staticmethod
    def _send_sync(configuration: EmailConfiguration, recipient: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = _safe_header(subject, 180)
        message["From"] = formataddr((configuration.from_name, configuration.from_email))
        message["To"] = recipient
        message.set_content(str(body or "")[:12000])
        context = ssl.create_default_context()
        if configuration.security == "tls":
            server: smtplib.SMTP = smtplib.SMTP_SSL(
                configuration.host,
                configuration.port,
                timeout=configuration.timeout_seconds,
                context=context,
            )
        else:
            server = smtplib.SMTP(configuration.host, configuration.port, timeout=configuration.timeout_seconds)
        with server:
            server.ehlo()
            if configuration.security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if configuration.username:
                server.login(configuration.username, configuration.password)
            server.send_message(message)
