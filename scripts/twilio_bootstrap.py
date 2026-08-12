#!/usr/bin/env python3
"""Idempotently provision a Twilio Elastic SIP Trunk for Floodman.

The script deliberately uses Twilio's REST endpoints directly so the runtime does
not need the Twilio SDK. It never prints API secrets or the SIP password.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
from envfile import EnvFileError, load_env_files

SID_PATTERNS = {
    "account": re.compile(r"^AC[0-9a-fA-F]{32}$"),
    "api_key": re.compile(r"^SK[0-9a-fA-F]{32}$"),
    "trunk": re.compile(r"^TK[0-9a-fA-F]{32}$"),
    "credential_list": re.compile(r"^CL[0-9a-fA-F]{32}$"),
    "credential": re.compile(r"^CR[0-9a-fA-F]{32}$"),
    "phone": re.compile(r"^PN[0-9a-fA-F]{32}$"),
    "origination": re.compile(r"^OU[0-9a-fA-F]{32}$"),
}
DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.pstn\.twilio\.com$")
USER_RE = re.compile(r"^[A-Za-z0-9_.@+\-]{3,32}$")
E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


class BootstrapError(RuntimeError):
    pass


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def require_sid(value: str, kind: str, *, optional: bool = False) -> str:
    if not value and optional:
        return ""
    if not SID_PATTERNS[kind].fullmatch(value):
        raise BootstrapError(f"Invalid {kind.replace('_', ' ')} SID")
    return value


def canonical_trunk_domain(termination_uri: str) -> str:
    """Return Twilio's canonical base trunk domain from a localized URI."""
    value = termination_uri.strip().lower()
    return re.sub(
        r"\.pstn\.[a-z0-9-]+\.twilio\.com$",
        ".pstn.twilio.com",
        value,
    )


def validate_password(value: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]{12,128}", value):
        raise BootstrapError(
            "TWILIO_SIP_PASSWORD must be 12-128 characters and use only letters, digits, period, underscore, or hyphen"
        )
    if not re.search(r"[a-z]", value) or not re.search(r"[A-Z]", value) or not re.search(r"\d", value):
        raise BootstrapError(
            "TWILIO_SIP_PASSWORD must include uppercase, lowercase, and a digit"
        )


def validate_origination_uri(uri: str, secure: bool) -> str:
    match = re.fullmatch(
        r"sip:(?:[^@;\s]+@)?(\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+)(?::([0-9]{1,5}))?(?:;[^\s]*)?",
        uri,
        flags=re.IGNORECASE,
    )
    if not match:
        raise BootstrapError("TWILIO_ORIGINATION_SIP_URI must be a sip: URI with a public IP or FQDN")
    hostname = match.group(1).strip("[]").lower()
    if hostname in {"127.0.0.1", "localhost", "0.0.0.0", "::1", "::"}:
        raise BootstrapError("TWILIO_ORIGINATION_SIP_URI must not point to a loopback or wildcard address")
    if match.group(2) and not 1 <= int(match.group(2)) <= 65535:
        raise BootstrapError("TWILIO_ORIGINATION_SIP_URI contains an invalid port")
    lowered = uri.lower()
    if secure and "transport=tls" not in lowered:
        uri = f"{uri};transport=tls"
    if not secure and "transport=tls" in lowered:
        raise BootstrapError("Origination URI requests TLS while TWILIO_SECURE_TRUNKING is false")
    return uri


@dataclass(slots=True)
class Desired:
    account_sid: str
    auth_username: str
    auth_password: str
    trunk_sid: str
    trunk_name: str
    domain_name: str
    secure: bool
    transfer_mode: str
    origination_sid: str
    origination_name: str
    origination_uri: str
    origination_priority: int
    origination_weight: int
    credential_list_sid: str
    credential_list_name: str
    credential_sid: str
    sip_username: str
    sip_password: str
    phone_number_sid: str
    phone_number: str
    allow_phone_routing_change: bool
    state_path: Path
    rotate_password: bool

    @classmethod
    def from_env(cls) -> "Desired":
        api_region = env("TWILIO_API_REGION", "us1").lower()
        if api_region != "us1":
            raise BootstrapError(
                "Automated provisioning currently targets Twilio US1 only; configure non-US regional trunks in Twilio Console"
            )
        account_sid = require_sid(env("TWILIO_ACCOUNT_SID"), "account")
        api_key = env("TWILIO_API_KEY")
        api_secret = env("TWILIO_API_KEY_SECRET")
        auth_token = env("TWILIO_AUTH_TOKEN")
        if bool(api_key) != bool(api_secret):
            raise BootstrapError("TWILIO_API_KEY and TWILIO_API_KEY_SECRET must be supplied together")
        if api_key:
            require_sid(api_key, "api_key")
        if not api_key and not auth_token:
            raise BootstrapError("Set TWILIO_API_KEY/TWILIO_API_KEY_SECRET or TWILIO_AUTH_TOKEN")
        auth_username = api_key or account_sid
        auth_password = api_secret or auth_token

        domain = env("TWILIO_TRUNK_DOMAIN").lower()
        if not domain:
            domain = canonical_trunk_domain(env("TWILIO_TERMINATION_URI"))
        if not DOMAIN_RE.fullmatch(domain):
            raise BootstrapError("TWILIO_TRUNK_DOMAIN must end in .pstn.twilio.com")
        secure = env_bool("TWILIO_SECURE_TRUNKING", False)
        origination = validate_origination_uri(env("TWILIO_ORIGINATION_SIP_URI"), secure)
        username = env("TWILIO_SIP_USERNAME")
        password = os.getenv("TWILIO_SIP_PASSWORD", "")
        if not USER_RE.fullmatch(username):
            raise BootstrapError("TWILIO_SIP_USERNAME must be 3-32 safe characters")
        validate_password(password)
        transfer_mode = env("TWILIO_TRANSFER_MODE", "disable-all")
        if transfer_mode not in {"disable-all", "sip-only", "enable-all"}:
            raise BootstrapError("TWILIO_TRANSFER_MODE must be disable-all, sip-only, or enable-all")
        try:
            priority = int(env("TWILIO_ORIGINATION_PRIORITY", "10"))
            weight = int(env("TWILIO_ORIGINATION_WEIGHT", "10"))
        except ValueError as exc:
            raise BootstrapError("Twilio origination priority and weight must be integers") from exc
        if not (0 <= priority <= 65535 and 1 <= weight <= 65535):
            raise BootstrapError("Twilio origination priority must be 0-65535 and weight 1-65535")
        phone_number = env("TWILIO_PHONE_NUMBER", env("TWILIO_FROM_NUMBER"))
        if phone_number and not E164_RE.fullmatch(phone_number):
            raise BootstrapError("TWILIO_PHONE_NUMBER must use E.164 with a leading +")
        data_dir = Path(env("DATA_DIR", str(PROJECT_ROOT / "data")))
        return cls(
            account_sid=account_sid,
            auth_username=auth_username,
            auth_password=auth_password,
            trunk_sid=require_sid(env("TWILIO_TRUNK_SID"), "trunk", optional=True),
            trunk_name=env("TWILIO_TRUNK_FRIENDLY_NAME", "Floodman Operations Voice"),
            domain_name=domain,
            secure=secure,
            transfer_mode=transfer_mode,
            origination_sid=require_sid(
                env("TWILIO_ORIGINATION_URL_SID"), "origination", optional=True
            ),
            origination_name=env("TWILIO_ORIGINATION_FRIENDLY_NAME", "Floodman Primary Asterisk"),
            origination_uri=origination,
            origination_priority=priority,
            origination_weight=weight,
            credential_list_sid=require_sid(
                env("TWILIO_CREDENTIAL_LIST_SID"), "credential_list", optional=True
            ),
            credential_list_name=env(
                "TWILIO_CREDENTIAL_LIST_FRIENDLY_NAME", "Floodman Asterisk Credentials"
            ),
            credential_sid=require_sid(env("TWILIO_CREDENTIAL_SID"), "credential", optional=True),
            sip_username=username,
            sip_password=password,
            phone_number_sid=require_sid(env("TWILIO_PHONE_NUMBER_SID"), "phone", optional=True),
            phone_number=phone_number,
            allow_phone_routing_change=env_bool("TWILIO_ALLOW_PHONE_ROUTING_CHANGE", False),
            state_path=Path(env("TWILIO_BOOTSTRAP_STATE", str(data_dir / "twilio/provisioning.json"))),
            rotate_password=env_bool("TWILIO_ROTATE_SIP_PASSWORD_ON_APPLY", False),
        )

    def public_summary(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("auth_password", None)
        value.pop("sip_password", None)
        value["auth_username"] = "API key" if self.auth_username.startswith("SK") else "Account SID"
        value["state_path"] = str(self.state_path)
        return value


class TwilioAPI:
    def __init__(self, desired: Desired):
        self.desired = desired
        self.client = httpx.Client(
            auth=(desired.auth_username, desired.auth_password),
            timeout=httpx.Timeout(20.0, connect=8.0),
            headers={"User-Agent": "Floodman-Operations-Voice/1.1.1"},
            follow_redirects=False,
        )

    def close(self) -> None:
        self.client.close()

    def request(
        self,
        method: str,
        url: str,
        *,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self.client.request(method, url, data=data, params=params)
        if response.status_code >= 400:
            message = ""
            try:
                payload = response.json()
                message = str(payload.get("message") or payload.get("detail") or "")
            except Exception:
                message = response.text[:300]
            raise BootstrapError(f"Twilio API {method} {url} returned HTTP {response.status_code}: {message}")
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    @property
    def account_base(self) -> str:
        return f"https://api.twilio.com/2010-04-01/Accounts/{self.desired.account_sid}"

    @staticmethod
    def trunk_base() -> str:
        return "https://trunking.twilio.com/v1"

    def list_trunks(self) -> list[dict[str, Any]]:
        return self.request("GET", f"{self.trunk_base()}/Trunks?PageSize=1000").get("trunks", [])

    def fetch_trunk(self, sid: str) -> dict[str, Any]:
        return self.request("GET", f"{self.trunk_base()}/Trunks/{sid}")

    def list_credential_lists(self) -> list[dict[str, Any]]:
        payload = self.request("GET", f"{self.account_base}/SIP/CredentialLists.json?PageSize=1000")
        return payload.get("credential_lists", [])

    def list_credentials(self, list_sid: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            f"{self.account_base}/SIP/CredentialLists/{list_sid}/Credentials.json?PageSize=1000",
        )
        return payload.get("credentials", [])

    def list_trunk_credential_lists(self, trunk_sid: str) -> list[dict[str, Any]]:
        return self.request(
            "GET", f"{self.trunk_base()}/Trunks/{trunk_sid}/CredentialLists?PageSize=1000"
        ).get("credential_lists", [])

    def list_origination_urls(self, trunk_sid: str) -> list[dict[str, Any]]:
        return self.request(
            "GET", f"{self.trunk_base()}/Trunks/{trunk_sid}/OriginationUrls?PageSize=1000"
        ).get("origination_urls", [])

    def list_phone_numbers(self, trunk_sid: str) -> list[dict[str, Any]]:
        return self.request(
            "GET", f"{self.trunk_base()}/Trunks/{trunk_sid}/PhoneNumbers?PageSize=1000"
        ).get("phone_numbers", [])

    def list_incoming_phone_numbers(self, phone_number: str) -> list[dict[str, Any]]:
        payload = self.request(
            "GET",
            f"{self.account_base}/IncomingPhoneNumbers.json",
            params={"PhoneNumber": phone_number, "PageSize": 1000},
        )
        return payload.get("incoming_phone_numbers", [])

    def fetch_incoming_phone_number(self, phone_number_sid: str) -> dict[str, Any]:
        return self.request(
            "GET",
            f"{self.account_base}/IncomingPhoneNumbers/{phone_number_sid}.json",
        )


@dataclass(slots=True)
class PlanAction:
    action: str
    resource: str
    detail: str
    changes: dict[str, Any]


def choose(items: list[dict[str, Any]], *, sid: str = "", **criteria: str) -> dict[str, Any] | None:
    if sid:
        return next((item for item in items if item.get("sid") == sid), None)
    for item in items:
        if all(str(item.get(key, "")).lower() == str(value).lower() for key, value in criteria.items()):
            return item
    return None


def resolve_phone_number(api: TwilioAPI, desired: Desired) -> tuple[str, dict[str, Any]]:
    if desired.phone_number_sid:
        row = api.fetch_incoming_phone_number(desired.phone_number_sid)
        resolved = require_sid(str(row.get("sid", "")), "phone")
        actual_number = str(row.get("phone_number", ""))
        if desired.phone_number and actual_number != desired.phone_number:
            raise BootstrapError(
                "TWILIO_PHONE_NUMBER and TWILIO_PHONE_NUMBER_SID refer to different resources"
            )
    elif desired.phone_number:
        rows = api.list_incoming_phone_numbers(desired.phone_number)
        exact = [row for row in rows if str(row.get("phone_number", "")) == desired.phone_number]
        if not exact:
            raise BootstrapError(
                f"Twilio number {desired.phone_number} was not found in this account"
            )
        if len(exact) > 1:
            raise BootstrapError(
                f"Twilio number {desired.phone_number} matched multiple resources; set TWILIO_PHONE_NUMBER_SID"
            )
        row = exact[0]
        resolved = require_sid(str(row.get("sid", "")), "phone")
    else:
        return "", {}

    capabilities = row.get("capabilities") or {}
    if isinstance(capabilities, dict) and capabilities and not bool(capabilities.get("voice")):
        number = desired.phone_number or str(row.get("phone_number", ""))
        raise BootstrapError(f"Twilio number {number} does not have Voice capability")
    return resolved, row


def _phone_routing_summary(row: dict[str, Any]) -> dict[str, str]:
    return {
        "trunk_sid": str(row.get("trunk_sid") or ""),
        "voice_url": str(row.get("voice_url") or ""),
        "voice_application_sid": str(row.get("voice_application_sid") or ""),
    }


def build_plan(
    api: TwilioAPI,
    desired: Desired,
    *,
    include_password_rotation: bool = True,
) -> tuple[list[PlanAction], dict[str, str]]:
    actions: list[PlanAction] = []
    ids: dict[str, str] = {}
    phone_number_sid, phone_number_row = resolve_phone_number(api, desired)
    if phone_number_sid:
        ids["phone_number_sid"] = phone_number_sid

    trunks = api.list_trunks()
    trunk = choose(trunks, sid=desired.trunk_sid) or choose(trunks, domain_name=desired.domain_name)
    if trunk is None:
        actions.append(PlanAction("create", "trunk", desired.trunk_name, {
            "FriendlyName": desired.trunk_name,
            "DomainName": desired.domain_name,
            "Secure": desired.secure,
            "TransferMode": desired.transfer_mode,
            "CnamLookupEnabled": False,
        }))
    else:
        ids["trunk_sid"] = str(trunk["sid"])
        changes: dict[str, Any] = {}
        desired_fields = {
            "friendly_name": desired.trunk_name,
            "domain_name": desired.domain_name,
            "secure": desired.secure,
            "transfer_mode": desired.transfer_mode,
            "cnam_lookup_enabled": False,
        }
        for key, value in desired_fields.items():
            if trunk.get(key) != value:
                changes[key] = value
        if changes:
            actions.append(PlanAction("update", "trunk", str(trunk["sid"]), changes))
        else:
            actions.append(PlanAction("keep", "trunk", str(trunk["sid"]), {}))

    if phone_number_sid and phone_number_row:
        routing = _phone_routing_summary(phone_number_row)
        target_trunk_sid = ids.get("trunk_sid", "")
        routed_elsewhere = bool(
            (routing["trunk_sid"] and routing["trunk_sid"] != target_trunk_sid)
            or (
                not routing["trunk_sid"]
                and (routing["voice_url"] or routing["voice_application_sid"])
            )
        )
        if routed_elsewhere and not desired.allow_phone_routing_change:
            raise BootstrapError(
                "The Twilio number already has Voice routing. Review the current trunk/webhook/app and set "
                "TWILIO_ALLOW_PHONE_ROUTING_CHANGE=true only when replacing it is intentional"
            )

    credential_lists = api.list_credential_lists()
    credential_list = choose(credential_lists, sid=desired.credential_list_sid) or choose(
        credential_lists, friendly_name=desired.credential_list_name
    )
    if credential_list is None:
        actions.append(PlanAction("create", "credential_list", desired.credential_list_name, {}))
        actions.append(PlanAction("create", "credential", desired.sip_username, {}))
    else:
        ids["credential_list_sid"] = str(credential_list["sid"])
        actions.append(PlanAction("keep", "credential_list", str(credential_list["sid"]), {}))
        credentials = api.list_credentials(str(credential_list["sid"]))
        credential = choose(credentials, sid=desired.credential_sid) or choose(
            credentials, username=desired.sip_username
        )
        if credential is None:
            actions.append(PlanAction("create", "credential", desired.sip_username, {}))
        else:
            ids["credential_sid"] = str(credential["sid"])
            rotate = bool(desired.rotate_password and include_password_rotation)
            actions.append(PlanAction(
                "update" if rotate else "keep",
                "credential",
                str(credential["sid"]),
                {"password": "will be synchronized"} if rotate else {},
            ))

    if ids.get("trunk_sid") and ids.get("credential_list_sid"):
        mappings = api.list_trunk_credential_lists(ids["trunk_sid"])
        mapped = any(item.get("sid") == ids["credential_list_sid"] for item in mappings)
        actions.append(PlanAction("keep" if mapped else "attach", "credential_list_mapping", ids["credential_list_sid"], {}))

    if ids.get("trunk_sid"):
        origination_urls = api.list_origination_urls(ids["trunk_sid"])
        origin = choose(origination_urls, sid=desired.origination_sid) or choose(
            origination_urls, friendly_name=desired.origination_name
        ) or choose(origination_urls, sip_url=desired.origination_uri)
        if origin is None:
            actions.append(PlanAction("create", "origination_url", desired.origination_uri, {}))
        else:
            ids["origination_url_sid"] = str(origin["sid"])
            changes = {}
            expected = {
                "friendly_name": desired.origination_name,
                "sip_url": desired.origination_uri,
                "priority": desired.origination_priority,
                "weight": desired.origination_weight,
                "enabled": True,
            }
            for key, value in expected.items():
                if origin.get(key) != value:
                    changes[key] = value
            actions.append(PlanAction("update" if changes else "keep", "origination_url", str(origin["sid"]), changes))

        if phone_number_sid:
            numbers = api.list_phone_numbers(ids["trunk_sid"])
            associated = any(
                item.get("sid") == phone_number_sid
                or item.get("phone_number_sid") == phone_number_sid
                for item in numbers
            )
            actions.append(PlanAction("keep" if associated else "attach", "phone_number", phone_number_sid, {}))

    return actions, ids


def apply(api: TwilioAPI, desired: Desired) -> dict[str, str]:
    actions, ids = build_plan(api, desired)
    phone_number_sid = ids.get("phone_number_sid", "")
    trunk_action = next(item for item in actions if item.resource == "trunk")
    if trunk_action.action == "create":
        trunk = api.request("POST", f"{api.trunk_base()}/Trunks", data={
            "FriendlyName": desired.trunk_name,
            "DomainName": desired.domain_name,
            "Secure": str(desired.secure).lower(),
            "TransferMode": desired.transfer_mode,
            "CnamLookupEnabled": "false",
        })
        ids["trunk_sid"] = str(trunk["sid"])
    elif trunk_action.action == "update":
        trunk = api.request("POST", f"{api.trunk_base()}/Trunks/{ids['trunk_sid']}", data={
            "FriendlyName": desired.trunk_name,
            "DomainName": desired.domain_name,
            "Secure": str(desired.secure).lower(),
            "TransferMode": desired.transfer_mode,
            "CnamLookupEnabled": "false",
        })
        ids["trunk_sid"] = str(trunk["sid"])

    list_action = next(item for item in actions if item.resource == "credential_list")
    if list_action.action == "create":
        created = api.request("POST", f"{api.account_base}/SIP/CredentialLists.json", data={
            "FriendlyName": desired.credential_list_name,
        })
        ids["credential_list_sid"] = str(created["sid"])

    list_sid = ids["credential_list_sid"]
    credentials = api.list_credentials(list_sid)
    credential = choose(credentials, sid=desired.credential_sid) or choose(
        credentials, username=desired.sip_username
    )
    if credential is None:
        created = api.request(
            "POST",
            f"{api.account_base}/SIP/CredentialLists/{list_sid}/Credentials.json",
            data={"Username": desired.sip_username, "Password": desired.sip_password},
        )
        ids["credential_sid"] = str(created["sid"])
    else:
        ids["credential_sid"] = str(credential["sid"])
        if desired.rotate_password:
            api.request(
                "POST",
                f"{api.account_base}/SIP/CredentialLists/{list_sid}/Credentials/{ids['credential_sid']}.json",
                data={"Password": desired.sip_password},
            )

    mappings = api.list_trunk_credential_lists(ids["trunk_sid"])
    if not any(item.get("sid") == ids["credential_list_sid"] for item in mappings):
        api.request(
            "POST",
            f"{api.trunk_base()}/Trunks/{ids['trunk_sid']}/CredentialLists",
            data={"CredentialListSid": ids["credential_list_sid"]},
        )

    origin_urls = api.list_origination_urls(ids["trunk_sid"])
    origin = choose(origin_urls, sid=desired.origination_sid) or choose(
        origin_urls, friendly_name=desired.origination_name
    ) or choose(origin_urls, sip_url=desired.origination_uri)
    origin_data = {
        "FriendlyName": desired.origination_name,
        "SipUrl": desired.origination_uri,
        "Priority": str(desired.origination_priority),
        "Weight": str(desired.origination_weight),
        "Enabled": "true",
    }
    if origin is None:
        created = api.request(
            "POST", f"{api.trunk_base()}/Trunks/{ids['trunk_sid']}/OriginationUrls", data=origin_data
        )
        ids["origination_url_sid"] = str(created["sid"])
    else:
        ids["origination_url_sid"] = str(origin["sid"])
        api.request(
            "POST",
            f"{api.trunk_base()}/Trunks/{ids['trunk_sid']}/OriginationUrls/{ids['origination_url_sid']}",
            data=origin_data,
        )

    if phone_number_sid:
        numbers = api.list_phone_numbers(ids["trunk_sid"])
        if not any(
            item.get("sid") == phone_number_sid
            or item.get("phone_number_sid") == phone_number_sid
            for item in numbers
        ):
            api.request(
                "POST",
                f"{api.trunk_base()}/Trunks/{ids['trunk_sid']}/PhoneNumbers",
                data={"PhoneNumberSid": phone_number_sid},
            )
        ids["phone_number_sid"] = phone_number_sid

    desired.state_path.parent.mkdir(parents=True, exist_ok=True)
    desired.state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "trunk_sid": ids.get("trunk_sid", ""),
                "credential_list_sid": ids.get("credential_list_sid", ""),
                "credential_sid": ids.get("credential_sid", ""),
                "origination_url_sid": ids.get("origination_url_sid", ""),
                "phone_number_sid": ids.get("phone_number_sid", ""),
                "domain_name": desired.domain_name,
                "origination_uri": desired.origination_uri,
                "secure": desired.secure,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    desired.state_path.chmod(0o600)
    return ids


def verify(api: TwilioAPI, desired: Desired) -> dict[str, Any]:
    actions, ids = build_plan(api, desired, include_password_rotation=False)
    pending = [asdict(item) for item in actions if item.action not in {"keep"}]
    trunk = api.fetch_trunk(ids["trunk_sid"]) if ids.get("trunk_sid") else {}
    return {
        "ok": not pending,
        "pending_actions": pending,
        "ids": ids,
        "trunk": {
            "sid": trunk.get("sid", ""),
            "friendly_name": trunk.get("friendly_name", ""),
            "domain_name": trunk.get("domain_name", ""),
            "secure": trunk.get("secure"),
            "auth_type": trunk.get("auth_type", ""),
            "transfer_mode": trunk.get("transfer_mode", ""),
        },
        "origination_uri": desired.origination_uri,
        "phone_number_attached": bool(ids.get("phone_number_sid") and ids.get("trunk_sid") and not any(
            item["resource"] == "phone_number" and item["action"] == "attach" for item in pending
        )),
    }


def print_json(value: Any) -> None:
    def redact(item: Any, *, key: str = "") -> Any:
        if isinstance(item, dict):
            return {sub_key: redact(sub_value, key=sub_key) for sub_key, sub_value in item.items()}
        if isinstance(item, list):
            return [redact(entry, key=key) for entry in item]
        lowered = key.lower()
        if any(token in lowered for token in ("password", "secret", "token", "auth", "origination_uri")):
            return "******" if item not in {"", None} else item
        return item

    print(json.dumps(redact(value), indent=2, sort_keys=True, default=str))


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision Floodman's Twilio Elastic SIP Trunk")
    parser.add_argument(
        "--env-file",
        action="append",
        default=None,
        help="Load a dotenv file without executing it; may be repeated (default: PROJECT_ROOT/.env)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="Read Twilio state and show changes without writing")
    apply_parser = sub.add_parser("apply", help="Create or update the trunk")
    apply_parser.add_argument("--yes", action="store_true", help="Required acknowledgement for write operations")
    sub.add_parser("verify", help="Verify that current Twilio state matches the requested configuration")
    sub.add_parser("show-config", help="Validate and print a secret-free configuration summary")
    args = parser.parse_args()

    try:
        env_files = args.env_file if args.env_file is not None else [str(PROJECT_ROOT / ".env")]
        load_env_files(env_files)
        desired = Desired.from_env()
        if args.command == "show-config":
            print_json({"ok": True, "desired": desired.public_summary()})
            return 0
        if args.command == "apply" and not args.yes:
            raise BootstrapError("apply requires --yes")
        api = TwilioAPI(desired)
        try:
            if args.command == "plan":
                actions, ids = build_plan(api, desired)
                print_json({
                    "ok": True,
                    "desired": desired.public_summary(),
                    "known_ids": ids,
                    "actions": [asdict(item) for item in actions],
                })
                return 0
            if args.command == "apply":
                ids = apply(api, desired)
                result = verify(api, desired)
                result["applied"] = True
                result["ids"] = ids
                result["state_path"] = str(desired.state_path)
                print_json(result)
                return 0 if result["ok"] else 2
            result = verify(api, desired)
            print_json(result)
            return 0 if result["ok"] else 2
        finally:
            api.close()
    except (BootstrapError, EnvFileError, httpx.HTTPError) as exc:
        print_json({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
