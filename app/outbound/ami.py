from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass

from app.config import Settings

_SAFE_VALUE = re.compile(r"^[^\r\n]*$")
_SAFE_AGENT = re.compile(r"^[a-z0-9_\-]{1,64}$")


@dataclass(slots=True)
class OriginateResult:
    ok: bool
    action_id: str
    message: str
    response: dict[str, str]
    answered: bool | None = None
    channel: str = ""
    reason_code: str = ""


class AMIError(RuntimeError):
    """Raised when Asterisk Manager Interface rejects an operation."""


class AMIClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    async def _read_block(reader: asyncio.StreamReader, timeout: float) -> dict[str, str]:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        result: dict[str, str] = {}
        for line in raw.decode("utf-8", errors="replace").split("\r\n"):
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            result[key.strip()] = value.strip()
        return result

    @staticmethod
    async def _send_action(
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        fields: list[tuple[str, str]],
        timeout: float,
    ) -> dict[str, str]:
        for _, value in fields:
            if not _SAFE_VALUE.match(value):
                raise ValueError("AMI field values may not contain CR or LF characters")
        payload = "".join(f"{key}: {value}\r\n" for key, value in fields) + "\r\n"
        writer.write(payload.encode("utf-8"))
        await writer.drain()

        expected_action_id = next(
            (
                value
                for key, value in fields
                if key.strip().lower() == "actionid"
            ),
            "",
        )
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise asyncio.TimeoutError(
                    "Timed out waiting for AMI action response"
                )
            block = await AMIClient._read_block(reader, remaining)
            if not block.get("Response"):
                continue
            response_action_id = block.get("ActionID", "")
            if (
                expected_action_id
                and response_action_id
                and response_action_id != expected_action_id
            ):
                continue
            return block

    @staticmethod
    async def _wait_for_originate_response(
        reader: asyncio.StreamReader, action_id: str, timeout: float
    ) -> dict[str, str] | None:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            try:
                block = await AMIClient._read_block(reader, remaining)
            except asyncio.TimeoutError:
                return None
            if (
                block.get("Event", "").lower() == "originateresponse"
                and block.get("ActionID") == action_id
            ):
                return block

    async def ping(self) -> dict[str, object]:
        """Authenticate to AMI and issue a Ping without exposing credentials."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.settings.ami_host, self.settings.ami_port),
            timeout=min(5.0, self.settings.ami_timeout_seconds),
        )
        try:
            banner = await asyncio.wait_for(reader.readline(), timeout=3.0)
            if not banner.lower().startswith(b"asterisk call manager"):
                raise AMIError("Unexpected AMI banner")
            login = await self._send_action(
                writer,
                reader,
                [
                    ("Action", "Login"),
                    ("Username", self.settings.ami_username),
                    ("Secret", self.settings.ami_secret),
                    ("Events", "off"),
                    ("ActionID", "floodman-health-login"),
                ],
                min(5.0, self.settings.ami_timeout_seconds),
            )
            if login.get("Response", "").lower() != "success":
                raise AMIError(login.get("Message", "AMI login failed"))
            pong = await self._send_action(
                writer,
                reader,
                [("Action", "Ping"), ("ActionID", "floodman-health-ping")],
                min(5.0, self.settings.ami_timeout_seconds),
            )
            return {
                "ok": pong.get("Response", "").lower() == "success",
                "message": pong.get("Ping") or pong.get("Message") or pong.get("Response", ""),
            }
        finally:
            try:
                await self._send_action(
                    writer,
                    reader,
                    [("Action", "Logoff"), ("ActionID", "floodman-health-logoff")],
                    1.5,
                )
            except Exception:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def originate_test_call(self, *, phone: str, label: str = "production_audio_test") -> OriginateResult:
        """Call an allowlisted operator and enter the local Asterisk echo-test context."""
        phone_digits = re.sub(r"[^0-9+]", "", phone)
        if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone_digits):
            raise ValueError("Test destination must be an E.164 number including +")
        if not _SAFE_VALUE.match(label):
            raise ValueError("Test call label may not contain CR or LF characters")
        channel = f"PJSIP/{phone_digits}@{self.settings.asterisk_trunk}"
        action_id = str(uuid.uuid4())
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.settings.ami_host, self.settings.ami_port),
            timeout=min(10.0, self.settings.ami_timeout_seconds),
        )
        try:
            banner = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not banner.lower().startswith(b"asterisk call manager"):
                raise AMIError("Unexpected AMI banner")
            login = await self._send_action(
                writer,
                reader,
                [
                    ("Action", "Login"),
                    ("Username", self.settings.ami_username),
                    ("Secret", self.settings.ami_secret),
                    ("Events", "call"),
                    ("ActionID", f"login-{action_id}"),
                ],
                min(10.0, self.settings.ami_timeout_seconds),
            )
            if login.get("Response", "").lower() != "success":
                raise AMIError(login.get("Message", "AMI login failed"))
            caller_id = self.settings.outbound_caller_id_name
            if self.settings.outbound_caller_id_number:
                caller_id = (
                    f'"{self.settings.outbound_caller_id_name}" '
                    f'<{self.settings.outbound_caller_id_number}>'
                )
            queued = await self._send_action(
                writer,
                reader,
                [
                    ("Action", "Originate"),
                    ("ActionID", action_id),
                    ("Channel", channel),
                    ("Context", "floodman-test-call"),
                    ("Exten", "s"),
                    ("Priority", "1"),
                    ("CallerID", caller_id),
                    ("Timeout", str(int(self.settings.ami_timeout_seconds * 1000))),
                    ("Async", "true"),
                    ("Variable", "FLOODMAN_TEST_CALL=1"),
                    ("Variable", f"FLOODMAN_TEST_LABEL={label[:80]}"),
                ],
                min(10.0, self.settings.ami_timeout_seconds),
            )
            if queued.get("Response", "").lower() != "success":
                return OriginateResult(
                    ok=False,
                    action_id=action_id,
                    message=queued.get("Message", "AMI test originate rejected"),
                    response=queued,
                    answered=False,
                    channel=channel,
                )
            event = await self._wait_for_originate_response(
                reader, action_id, min(3.0, self.settings.ami_timeout_seconds)
            )
            if event is None:
                return OriginateResult(
                    ok=True,
                    action_id=action_id,
                    message="Test call queued; final AMI response timed out",
                    response=queued,
                    answered=None,
                    channel=channel,
                )
            answered = event.get("Response", "").lower() == "success"
            return OriginateResult(
                ok=answered,
                action_id=action_id,
                message=event.get("Message", event.get("Response", "")),
                response=event,
                answered=answered,
                channel=event.get("Channel", channel),
                reason_code=event.get("Reason", ""),
            )
        finally:
            try:
                await self._send_action(
                    writer,
                    reader,
                    [("Action", "Logoff"), ("ActionID", f"logoff-{action_id}")],
                    2.0,
                )
            except Exception:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def originate(
        self,
        *,
        phone: str,
        agent: str,
        job_id: str,
        purpose: str,
        customer_id: str = "",
        extra_variables: dict[str, str] | None = None,
    ) -> OriginateResult:
        if not _SAFE_AGENT.match(agent):
            raise ValueError(f"Unsafe AVA agent slug: {agent!r}")
        phone_digits = re.sub(r"[^0-9+]", "", phone)
        if not phone_digits:
            raise ValueError("Phone number is empty after normalization")
        template = str(
            self.settings.config.get("asterisk", {}).get(
                "outbound_channel_template", "PJSIP/{phone}@{trunk}"
            )
        )
        channel = template.format(phone=phone_digits, trunk=self.settings.asterisk_trunk)
        action_id = str(uuid.uuid4())
        variables = {
            "AI_AGENT": agent,
            # AVA retains AI_CONTEXT as a backwards-compatible routing alias.
            "AI_CONTEXT": agent,
            "AI_PROVIDER": self.settings.default_provider,
            # AVA maps these variables into post-call campaign/lead correlation.
            "AAVA_LEAD_ID": job_id,
            "AAVA_ATTEMPT_ID": action_id,
            "FLOODMAN_DIRECTION": "outbound",
            "FLOODMAN_JOB_ID": job_id,
            "FLOODMAN_PURPOSE": purpose,
            "FLOODMAN_CUSTOMER_ID": customer_id,
            "FLOODMAN_PHONE": phone_digits,
        }
        variables.update(extra_variables or {})

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.settings.ami_host, self.settings.ami_port),
            timeout=min(10.0, self.settings.ami_timeout_seconds),
        )
        try:
            banner = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if not banner.lower().startswith(b"asterisk call manager"):
                raise AMIError("Unexpected AMI banner")
            login = await self._send_action(
                writer,
                reader,
                [
                    ("Action", "Login"),
                    ("Username", self.settings.ami_username),
                    ("Secret", self.settings.ami_secret),
                    ("Events", "call"),
                    ("ActionID", f"login-{action_id}"),
                ],
                min(10.0, self.settings.ami_timeout_seconds),
            )
            if login.get("Response", "").lower() != "success":
                raise AMIError(login.get("Message", "AMI login failed"))

            caller_id = self.settings.outbound_caller_id_name
            if self.settings.outbound_caller_id_number:
                caller_id = (
                    f'"{self.settings.outbound_caller_id_name}" '
                    f"<{self.settings.outbound_caller_id_number}>"
                )
            fields: list[tuple[str, str]] = [
                ("Action", "Originate"),
                ("ActionID", action_id),
                ("Channel", channel),
                ("Application", "Stasis"),
                ("Data", self.settings.ava_stasis_app),
                ("CallerID", caller_id),
                ("Timeout", str(int(self.settings.ami_timeout_seconds * 1000))),
                ("Async", "true"),
            ]
            fields.extend(("Variable", f"{key}={value}") for key, value in variables.items())
            queued = await self._send_action(
                writer, reader, fields, min(10.0, self.settings.ami_timeout_seconds)
            )
            if queued.get("Response", "").lower() != "success":
                return OriginateResult(
                    ok=False,
                    action_id=action_id,
                    message=queued.get("Message", "AMI originate rejected"),
                    response=queued,
                    answered=False,
                    channel=channel,
                )

            event = await self._wait_for_originate_response(
                reader, action_id, min(3.0, self.settings.ami_timeout_seconds)
            )
            if event is None:
                return OriginateResult(
                    ok=True,
                    action_id=action_id,
                    message="Originate queued; final AMI response timed out",
                    response=queued,
                    answered=None,
                    channel=channel,
                )
            answered = event.get("Response", "").lower() == "success"
            return OriginateResult(
                ok=answered,
                action_id=action_id,
                message=event.get("Message", event.get("Response", "")),
                response=event,
                answered=answered,
                channel=event.get("Channel", channel),
                reason_code=event.get("Reason", ""),
            )
        finally:
            try:
                await self._send_action(
                    writer,
                    reader,
                    [("Action", "Logoff"), ("ActionID", f"logoff-{action_id}")],
                    2.0,
                )
            except Exception:
                pass
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
