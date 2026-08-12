#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Make the script runnable by absolute path, from CI, or from an arbitrary shell directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Settings
from app.diagnostics import static_configuration_checks
from envfile import EnvFileError, load_env_files

E164 = re.compile(r"^\+[1-9][0-9]{7,14}$")
SIP_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)$")


def parse_sip_uri(value: str) -> tuple[str, int | None, dict[str, str]] | None:
    raw = value.strip()
    if not raw.lower().startswith("sip:"):
        return None
    remainder = raw[4:].split("?", 1)[0]
    if not remainder or any(char.isspace() for char in remainder):
        return None

    address, _, params_text = remainder.partition(";")
    if "@" in address:
        _, address = address.rsplit("@", 1)
    if not address:
        return None

    port: int | None = None
    if address.startswith("["):
        closing = address.find("]")
        if closing <= 0:
            return None
        host = address[1:closing].lower()
        if not SIP_HOST.fullmatch(f"[{host}]"):
            return None
        suffix = address[closing + 1 :]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:].isdigit():
                return None
            port = int(suffix[1:])
    else:
        if ":" in address:
            host, port_text = address.rsplit(":", 1)
            if not host or not port_text.isdigit():
                return None
            host = host.lower()
            port = int(port_text)
        else:
            host = address.lower()
        if not SIP_HOST.fullmatch(host):
            return None

    if port is not None and not 1 <= port <= 65535:
        return None

    params: dict[str, str] = {}
    for item in params_text.split(";") if params_text else ():
        if not item:
            continue
        key, _, raw_value = item.partition("=")
        params[key.lower()] = raw_value.lower()
    return host, port, params


def result(name: str, ok: bool, severity: str, message: str, **metadata: Any) -> dict[str, Any]:
    return {
        "name": name,
        "ok": ok,
        "severity": severity,
        "message": message,
        "metadata": metadata,
    }


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def dns_check(hostname: str) -> tuple[bool, list[str] | str]:
    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(
            loop.run_in_executor(None, socket.getaddrinfo, hostname, None), timeout=5.0
        )
        addresses = sorted({str(row[4][0]) for row in records})
        return bool(addresses), addresses
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def validate(settings: Settings, *, require_provisioning: bool = False) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for name, item in static_configuration_checks(settings).items():
        checks.append(
            result(
                name,
                bool(item["ok"]),
                "error" if item["critical"] else "warning",
                str(item["detail"]),
                **dict(item.get("metadata") or {}),
            )
        )

    public_ip_value = os.getenv("PUBLIC_IP", "").strip()
    parsed_public_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    if settings.sip_trunk_mode != "disabled":
        try:
            parsed_public_ip = ipaddress.ip_address(public_ip_value)
            globally_routable = parsed_public_ip.is_global
            checks.append(
                result(
                    "public_ip",
                    globally_routable,
                    "error",
                    "PUBLIC_IP is globally routable"
                    if globally_routable
                    else "PUBLIC_IP must be a globally routable address, not a private, loopback, or documentation address",
                    version=parsed_public_ip.version,
                    address=str(parsed_public_ip),
                )
            )
        except ValueError:
            checks.append(
                result(
                    "public_ip",
                    False,
                    "error",
                    "PUBLIC_IP must be the public address advertised in SIP and SDP",
                )
            )

    try:
        rtp_start = int(os.getenv("RTP_START", "10000"))
        rtp_end = int(os.getenv("RTP_END", "10040"))
        count = rtp_end - rtp_start + 1
        checks.append(
            result(
                "rtp_range",
                1024 <= rtp_start <= rtp_end <= 65535 and count >= 20,
                "error",
                f"RTP range contains {count} UDP ports",
                start=rtp_start,
                end=rtp_end,
            )
        )
    except ValueError:
        checks.append(result("rtp_range", False, "error", "RTP_START and RTP_END must be integers"))

    for name in (
        "FLOODMAN_LIVE_NUMBER",
        "FLOODMAN_EMERGENCY_NUMBER",
        "FLOODMAN_BILLING_NUMBER",
        "FLOODMAN_ESTIMATING_NUMBER",
        "OUTBOUND_CALLER_ID_NUMBER",
    ):
        value = os.getenv(name, "").strip()
        if value:
            valid = bool(E164.fullmatch(value))
            checks.append(
                result(
                    name.lower(),
                    valid,
                    "error",
                    f"{name} uses E.164 with a leading +"
                    if valid
                    else f"{name} must use E.164 with a leading +",
                )
            )

    if settings.test_calls_enabled:
        allowlist = settings.test_call_allowlist
        good = bool(allowlist) and all(E164.fullmatch(value) for value in allowlist)
        checks.append(
            result(
                "test_call_allowlist",
                good,
                "error",
                "Test calls are restricted to an explicit E.164 allowlist" if good else "TEST_CALL_ALLOWLIST must contain at least one E.164 number",
                count=len(allowlist),
            )
        )

    parsed = urlparse(settings.public_base_url)
    if parsed.hostname:
        checks.append(
            result(
                "trusted_host_matches_public_url",
                parsed.hostname in settings.trusted_hosts,
                "error",
                "PUBLIC_BASE_URL host is present in TRUSTED_HOSTS",
                host=parsed.hostname,
            )
        )

    if settings.sip_trunk_mode == "twilio":
        secure = env_bool("TWILIO_SECURE_TRUNKING")
        sip_port = int(os.getenv("SIP_TLS_PORT" if secure else "SIP_PORT", "5061" if secure else "5060"))
        origination_uri = os.getenv("TWILIO_ORIGINATION_SIP_URI", "").strip()
        parsed_origination = parse_sip_uri(origination_uri) if origination_uri else None
        origination_ok = parsed_origination is not None
        if parsed_origination is not None:
            origin_host, origin_port, origin_params = parsed_origination
            transport = origin_params.get("transport", "udp")
            if secure:
                origination_ok = transport == "tls"
            elif transport == "tls":
                origination_ok = False
            actual_port = origin_port or (5061 if transport == "tls" else 5060)
            expected_host = os.getenv("PUBLIC_SIP_HOST", "").strip().lower() or public_ip_value.lower()
            target_ok = bool(expected_host) and origin_host == expected_host and actual_port == sip_port
            checks.append(
                result(
                    "twilio_origination_target",
                    target_ok,
                    "error",
                    "Twilio origination URI targets this server's advertised SIP host and port"
                    if target_ok
                    else "TWILIO_ORIGINATION_SIP_URI host and port must match PUBLIC_SIP_HOST or PUBLIC_IP and the active SIP port",
                    host=origin_host,
                    port=actual_port,
                    expected_host=expected_host,
                    expected_port=sip_port,
                    edge=origin_params.get("edge", origin_params.get("region", "")),
                )
            )
        checks.append(
            result(
                "twilio_origination_uri",
                origination_ok,
                "error" if require_provisioning else "warning",
                "TWILIO_ORIGINATION_SIP_URI is valid for automated provisioning"
                if origination_ok
                else "Set a valid public Twilio origination SIP URI; secure trunks must include transport=tls",
                expected=f"sip:{os.getenv('PUBLIC_SIP_HOST', os.getenv('PUBLIC_IP', 'PUBLIC_IP'))}:{sip_port}"
                + (";transport=tls" if secure else ""),
            )
        )
        twilio_number = os.getenv("TWILIO_PHONE_NUMBER", "").strip()
        checks.append(
            result(
                "twilio_phone_number",
                bool(E164.fullmatch(twilio_number)),
                "error",
                "TWILIO_PHONE_NUMBER is an E.164 DID"
                if E164.fullmatch(twilio_number)
                else "Set the owned Twilio DID in TWILIO_PHONE_NUMBER using E.164",
            )
        )
        domain = os.getenv("TWILIO_TRUNK_DOMAIN", "").strip().lower()
        termination = os.getenv("TWILIO_TERMINATION_URI", "").strip().lower()
        canonical_termination = re.sub(
            r"\.pstn\.[A-Za-z0-9-]+\.twilio\.com$",
            ".pstn.twilio.com",
            termination,
        )
        if domain and termination:
            aligned = domain == canonical_termination
            checks.append(result(
                "twilio_domain_alignment",
                aligned,
                "error",
                "Twilio trunk domain matches the base or localized Asterisk termination URI"
                if aligned
                else "TWILIO_TRUNK_DOMAIN must match the base domain of TWILIO_TERMINATION_URI",
                canonical_termination=canonical_termination,
            ))
        elif require_provisioning:
            checks.append(
                result(
                    "twilio_domain_alignment",
                    False,
                    "error",
                    "TWILIO_TRUNK_DOMAIN is required for provisioning preflight",
                    canonical_termination=canonical_termination,
                )
            )

        if require_provisioning:
            account_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
            api_key = os.getenv("TWILIO_API_KEY", "").strip()
            api_secret = os.getenv("TWILIO_API_KEY_SECRET", "")
            auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
            account_ok = bool(re.fullmatch(r"AC[0-9a-fA-F]{32}", account_sid))
            api_key_ok = bool(
                re.fullmatch(r"SK[0-9a-fA-F]{32}", api_key) and api_secret
            )
            fallback_ok = bool(auth_token and not api_key and not api_secret)
            credentials_ok = account_ok and (api_key_ok or fallback_ok)
            checks.append(
                result(
                    "twilio_provisioning_credentials",
                    credentials_ok,
                    "error",
                    "Twilio provisioning credentials are structurally valid"
                    if credentials_ok
                    else "Provisioning requires a valid Account SID and either an API key pair or temporary Auth Token fallback",
                    api_key_configured=api_key_ok,
                    auth_token_fallback=bool(fallback_ok),
                )
            )
            region = os.getenv("TWILIO_API_REGION", "us1").strip().lower()
            checks.append(
                result(
                    "twilio_provisioning_region",
                    region == "us1",
                    "error",
                    "Automated provisioning region is US1"
                    if region == "us1"
                    else "Automated provisioning supports US1 only; configure other Twilio regions manually",
                    region=region,
                )
            )
        checks.append(
            result(
                "firewall_signaling",
                True,
                "info",
                "Allow Twilio signaling CIDRs to the configured SIP port; do not expose ARI or AMI publicly",
                sip_port=sip_port,
            )
        )
        checks.append(
            result(
                "firewall_media",
                True,
                "info",
                "Allow UDP from Twilio media range 168.86.128.0/18 to the local RTP range",
                twilio_media_cidr="168.86.128.0/18",
            )
        )

    return checks


async def main_async() -> int:
    parser = argparse.ArgumentParser(description="Validate Floodman Voice production configuration")
    parser.add_argument(
        "--env-file",
        action="append",
        default=None,
        help="Load a dotenv file without executing it; may be repeated (default: PROJECT_ROOT/.env)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when blocking checks fail")
    parser.add_argument(
        "--require-provisioning",
        action="store_true",
        help="Require the one-time Twilio provisioning fields and credentials",
    )
    parser.add_argument("--no-network", action="store_true", help="Skip DNS checks")
    args = parser.parse_args()

    env_files = args.env_file if args.env_file is not None else [str(PROJECT_ROOT / ".env")]
    try:
        load_env_files(env_files)
    except EnvFileError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"Environment file error: {exc}", file=sys.stderr)
        return 2

    settings = Settings.from_env()
    checks = validate(settings, require_provisioning=args.require_provisioning)
    if not args.no_network and settings.sip_trunk_mode == "twilio":
        host = os.getenv("TWILIO_TERMINATION_URI", "").strip().lower()
        if host:
            ok, details = await dns_check(host)
            checks.append(
                result(
                    "twilio_dns",
                    ok,
                    "error",
                    "Twilio termination URI resolves" if ok else str(details),
                    addresses=details if isinstance(details, list) else [],
                )
            )

    errors = [item for item in checks if not item["ok"] and item["severity"] == "error"]
    warnings = [item for item in checks if not item["ok"] and item["severity"] == "warning"]
    payload = {
        "ok": not errors,
        "version": "1.1.1",
        "errors": len(errors),
        "warnings": len(warnings),
        "checks": checks,
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        icons = {"error": "ERROR", "warning": "WARN", "info": "INFO"}
        for item in checks:
            state = "PASS" if item["ok"] else icons.get(item["severity"], "FAIL")
            print(f"[{state:5}] {item['name']}: {item['message']}")
        print(f"\nBlocking errors: {len(errors)} | Warnings: {len(warnings)}")
        if not errors:
            print("Production configuration preflight passed.")
    return 1 if args.strict and errors else 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
