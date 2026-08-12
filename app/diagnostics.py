from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from app.config import Settings
from app.db import Database
from app.outbound.ami import AMIClient

_WEAK_MARKERS = ("change-me", "changeme", "password", "secret", "token")
_E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")


def _component(
    ok: bool,
    status: str,
    *,
    critical: bool = False,
    detail: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "status": status,
        "critical": critical,
        "detail": detail,
        "metadata": metadata or {},
    }


def _looks_weak(value: str) -> bool:
    lowered = value.strip().lower()
    return not lowered or len(value) < 24 or any(marker == lowered for marker in _WEAK_MARKERS)


async def _tcp_probe(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        del reader
        return True, "connected"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _dns_probe(hostname: str, timeout: float = 4.0) -> tuple[bool, list[str] | str]:
    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, hostname, None, socket.AF_UNSPEC),
            timeout,
        )
        addresses = sorted({str(item[4][0]) for item in records})
        return bool(addresses), addresses
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def _ari_probe(settings: Settings) -> tuple[bool, str, dict[str, Any]]:
    if not settings.asterisk_ari_password:
        return False, "ARI password is not configured", {}
    url = (
        f"{settings.asterisk_ari_scheme}://{settings.asterisk_host}:"
        f"{settings.asterisk_ari_port}/ari/asterisk/info"
    )
    try:
        async with httpx.AsyncClient(
            timeout=4.0,
            verify=settings.asterisk_ari_scheme == "https",
        ) as client:
            response = await client.get(
                url,
                auth=(settings.asterisk_ari_username, settings.asterisk_ari_password),
                params={"only": "system,config,status,build"},
            )
        if response.status_code != 200:
            return False, f"ARI returned HTTP {response.status_code}", {}
        payload = response.json()
        metadata = {
            "version": payload.get("system", {}).get("version", ""),
            "entity_id": payload.get("system", {}).get("entity_id", ""),
            "startup_time": payload.get("status", {}).get("startup_time", ""),
        }
        return True, "authenticated", metadata
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


async def _ari_application_probe(settings: Settings) -> tuple[bool, str, dict[str, Any]]:
    if not settings.asterisk_ari_password:
        return False, "ARI password is not configured", {}
    app_name = quote(settings.ava_stasis_app, safe="")
    url = (
        f"{settings.asterisk_ari_scheme}://{settings.asterisk_host}:"
        f"{settings.asterisk_ari_port}/ari/applications/{app_name}"
    )
    try:
        async with httpx.AsyncClient(
            timeout=4.0,
            verify=settings.asterisk_ari_scheme == "https",
        ) as client:
            response = await client.get(
                url,
                auth=(settings.asterisk_ari_username, settings.asterisk_ari_password),
            )
        if response.status_code == 404:
            return False, f"ARI application {settings.ava_stasis_app} is not registered", {}
        if response.status_code != 200:
            return False, f"ARI application probe returned HTTP {response.status_code}", {}
        payload = response.json()
        metadata = {
            "name": payload.get("name", settings.ava_stasis_app),
            "channels": len(payload.get("channel_ids") or []),
            "bridges": len(payload.get("bridge_ids") or []),
            "endpoints": len(payload.get("endpoint_ids") or []),
        }
        return True, "AVA Stasis application is registered", metadata
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}", {}


def static_configuration_checks(settings: Settings) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    production = settings.environment.lower() == "production"

    parsed = urlparse(settings.public_base_url)
    https_ok = parsed.scheme == "https" or not production
    checks["public_url"] = _component(
        https_ok,
        "ready" if https_ok else "unsafe",
        critical=production,
        detail=(
            "Public URL uses HTTPS"
            if https_ok
            else "Production PUBLIC_BASE_URL must use HTTPS"
        ),
        metadata={"scheme": parsed.scheme, "host": parsed.hostname or ""},
    )

    secret_ok = not any(
        _looks_weak(value)
        for value in (settings.admin_token, settings.internal_token, settings.upload_token_secret)
    )
    checks["application_secrets"] = _component(
        secret_ok,
        "ready" if secret_ok else "unsafe",
        critical=True,
        detail="Application secrets meet the minimum length" if secret_ok else "One or more application secrets are blank or weak",
    )

    host_ok = bool(settings.trusted_hosts) and "*" not in settings.trusted_hosts
    checks["trusted_hosts"] = _component(
        host_ok,
        "ready" if host_ok else "unsafe",
        critical=production,
        detail="Host header allowlist configured" if host_ok else "TRUSTED_HOSTS must not contain * in production",
        metadata={"count": len(settings.trusted_hosts)},
    )

    proxy_ok = settings.trusted_proxy_ips not in {"", "*"} or not production
    checks["trusted_proxies"] = _component(
        proxy_ok,
        "ready" if proxy_ok else "unsafe",
        critical=production,
        detail="Proxy trust is restricted" if proxy_ok else "TRUSTED_PROXY_IPS must not be * in production",
    )

    local_net = os.getenv("LOCAL_NET", "172.16.0.0/12").strip()
    local_net_ok = local_net not in {"0.0.0.0/0", "::/0", "0.0.0.0/0,::/0"}
    checks["asterisk_local_net"] = _component(
        local_net_ok,
        "ready" if local_net_ok else "unsafe",
        critical=settings.asterisk_mode == "embedded",
        detail="Asterisk NAT local network is constrained" if local_net_ok else "LOCAL_NET cannot be the entire Internet",
        metadata={"local_net": local_net},
    )

    mode = settings.sip_trunk_mode
    mode_ok = mode in {"disabled", "registration", "ip", "twilio"}
    checks["sip_mode"] = _component(
        mode_ok,
        "ready" if mode_ok else "invalid",
        critical=True,
        detail=f"SIP trunk mode: {mode}",
    )

    if mode == "twilio":
        termination = os.getenv("TWILIO_TERMINATION_URI", "").strip().lower()
        user = os.getenv("TWILIO_SIP_USERNAME", "").strip()
        password = os.getenv("TWILIO_SIP_PASSWORD", "")
        public_ip = os.getenv("PUBLIC_IP", "").strip()
        from_number = os.getenv(
            "TWILIO_FROM_NUMBER", settings.outbound_caller_id_number
        ).strip()
        secure = os.getenv("TWILIO_SECURE_TRUNKING", "false").lower() in {
            "1", "true", "yes", "on"
        }
        try:
            parsed_public_ip = ipaddress.ip_address(public_ip)
            public_ip_ok = parsed_public_ip.is_global
        except ValueError:
            public_ip_ok = False
        twilio_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
        password_ok = bool(
            re.fullmatch(r"[A-Za-z0-9._-]{12,128}", password)
            and re.search(r"[a-z]", password)
            and re.search(r"[A-Z]", password)
            and re.search(r"[0-9]", password)
        )
        twilio_ok = bool(
            termination.endswith(".twilio.com")
            and ".pstn" in termination
            and user
            and password_ok
            and public_ip_ok
            and _E164.fullmatch(twilio_number)
            and (not from_number or _E164.fullmatch(from_number))
        )
        detail = "Twilio trunk credentials, owned DID, and public addressing are configured"
        if not twilio_ok:
            detail = (
                "Twilio mode requires a termination URI, strong SIP credentials, a globally routable "
                "PUBLIC_IP, an owned E.164 TWILIO_PHONE_NUMBER, and an E.164 From number"
            )
        checks["twilio_trunk"] = _component(
            twilio_ok,
            "ready" if twilio_ok else "incomplete",
            critical=True,
            detail=detail,
            metadata={
                "termination_uri": termination,
                "secure": secure,
                "public_ip_global": public_ip_ok,
                "phone_number_configured": bool(_E164.fullmatch(twilio_number)),
            },
        )
        symmetric_rtp = os.getenv("TWILIO_RTP_SYMMETRIC", "false").lower() in {
            "1", "true", "yes", "on"
        }
        checks["twilio_media_policy"] = _component(
            not symmetric_rtp,
            "ready" if not symmetric_rtp else "degraded",
            critical=False,
            detail=(
                "Twilio media uses the SDP destination advertised by Asterisk"
                if not symmetric_rtp
                else "Symmetric RTP is enabled; use it only as a diagnosed NAT workaround"
            ),
            metadata={"rtp_symmetric": symmetric_rtp},
        )
        if secure:
            cert = Path(os.getenv("SIP_TLS_CERT_FILE", ""))
            key = Path(os.getenv("SIP_TLS_KEY_FILE", ""))
            tls_ok = cert.is_file() and key.is_file()
            checks["sip_tls"] = _component(
                tls_ok,
                "ready" if tls_ok else "incomplete",
                critical=True,
                detail="TLS certificate and key are readable" if tls_ok else "Secure trunking requires SIP_TLS_CERT_FILE and SIP_TLS_KEY_FILE",
            )

    if settings.outbound_enabled or settings.test_calls_enabled:
        caller_ok = bool(_E164.fullmatch(settings.outbound_caller_id_number))
        checks["outbound_caller_id"] = _component(
            caller_ok,
            "ready" if caller_ok else "incomplete",
            critical=True,
            detail="Outbound caller ID is E.164" if caller_ok else "OUTBOUND_CALLER_ID_NUMBER must include + and use E.164",
        )

    return checks


async def collect_diagnostics(
    settings: Settings,
    *,
    database: Database | None = None,
    gate_server: object | None = None,
    ami: AMIClient | None = None,
    include_network: bool = True,
) -> dict[str, Any]:
    components = static_configuration_checks(settings)

    if database is not None:
        try:
            row = database.fetchone("SELECT 1 AS ok")
            db_ok = bool(row and row.get("ok") == 1)
            components["database"] = _component(
                db_ok,
                "ready" if db_ok else "failed",
                critical=True,
                detail="SQLite read/write connection is available" if db_ok else "SQLite probe returned an unexpected result",
            )
        except Exception as exc:
            components["database"] = _component(
                False, "failed", critical=True, detail=f"{type(exc).__name__}: {exc}"
            )

    gate_ok = not settings.gate_enabled or gate_server is not None
    components["call_gate"] = _component(
        gate_ok,
        "ready" if gate_ok else "failed",
        critical=settings.gate_enabled,
        detail=(
            "AudioSocket call gate is listening"
            if gate_server is not None
            else "Call gate disabled" if not settings.gate_enabled else "Call gate did not start"
        ),
        metadata={"port": settings.gate_port, "transcriber": settings.gate_transcriber},
    )

    if include_network and settings.asterisk_mode == "embedded":
        ari_ok, ari_detail, ari_metadata = await _ari_probe(settings)
        components["asterisk_ari"] = _component(
            ari_ok,
            "ready" if ari_ok else "failed",
            critical=settings.ava_enabled,
            detail=ari_detail,
            metadata=ari_metadata,
        )

        if settings.ava_enabled and ari_ok:
            app_ok, app_detail, app_metadata = await _ari_application_probe(settings)
            components["ava_stasis_application"] = _component(
                app_ok,
                "ready" if app_ok else "failed",
                critical=True,
                detail=app_detail,
                metadata=app_metadata,
            )

    if include_network and (settings.ami_enabled or settings.outbound_enabled or settings.test_calls_enabled):
        try:
            result = await (ami or AMIClient(settings)).ping()
            ami_ok = bool(result.get("ok"))
            components["asterisk_ami"] = _component(
                ami_ok,
                "ready" if ami_ok else "failed",
                critical=True,
                detail=str(result.get("message") or "AMI ping completed"),
            )
        except Exception as exc:
            components["asterisk_ami"] = _component(
                False, "failed", critical=True, detail=f"{type(exc).__name__}: {exc}"
            )

    if include_network and settings.sip_trunk_mode == "twilio":
        termination = os.getenv("TWILIO_TERMINATION_URI", "").strip().lower()
        if termination:
            dns_ok, dns_result = await _dns_probe(termination)
            components["twilio_dns"] = _component(
                dns_ok,
                "ready" if dns_ok else "failed",
                critical=True,
                detail="Twilio termination URI resolves" if dns_ok else str(dns_result),
                metadata={"addresses": dns_result if isinstance(dns_result, list) else []},
            )

    if settings.roomflow_enabled:
        roomflow_ok = bool(
            settings.roomflow_base_url
            and settings.roomflow_token
            and settings.roomflow_endpoints
        )
        components["roomflow"] = _component(
            roomflow_ok,
            "configured" if roomflow_ok else "incomplete",
            critical=False,
            detail="Roomflow adapter configured" if roomflow_ok else "Local ledger will queue writes until Roomflow is configured",
            metadata={"endpoint_mappings": len(settings.roomflow_endpoints)},
        )
    else:
        components["roomflow"] = _component(
            True,
            "disabled",
            critical=False,
            detail="Roomflow disabled; local ledger remains active",
        )

    blocking = [
        name for name, value in components.items() if value["critical"] and not value["ok"]
    ]
    warnings = [
        name for name, value in components.items() if not value["critical"] and not value["ok"]
    ]
    return {
        "ok": not blocking,
        "ready": not blocking,
        "blocking": blocking,
        "warnings": warnings,
        "components": components,
    }
