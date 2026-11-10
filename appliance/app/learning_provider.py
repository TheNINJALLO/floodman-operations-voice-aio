"""Account-only, bounded, read-only Outreach learning source."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import httpx

BASE = "https://production-api.outreachgenius.ai/api/v1/"


class SourceError(Exception):
    def __init__(self, code: str, delay: float = 60):
        self.code, self.delay = code, delay
        super().__init__(code)


def text(value, maximum=1000, required=False):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise SourceError("response_contract")
    return value


def envelope(value, expected):
    if not isinstance(value, dict) or value.get("success") is not True or value.get("error") is not None or not isinstance(value.get("data"), expected):
        raise SourceError("response_contract")
    return value["data"]


def page(value, collection, number, limit):
    body = envelope(value, dict)
    rows = body.get(collection)
    if not isinstance(rows, list) or not all(isinstance(v, dict) for v in rows) or len(rows) > limit:
        raise SourceError("response_contract")
    if type(body.get("page")) is not int or body["page"] != number or type(body.get("limit")) is not int or body["limit"] != limit or type(body.get("total")) is not int or body["total"] < 0:
        raise SourceError("pagination_contract")
    offset = (number - 1) * limit
    if offset + len(rows) > body["total"] or (not rows and offset < body["total"]):
        raise SourceError("pagination_contract")
    return rows, offset + len(rows) < body["total"]


def name_hashes(row):
    names = " ".join(text(row.get(k) or "", 160) for k in ("firstName", "lastName"))
    return [hashlib.sha256(v.casefold().encode()).hexdigest() for v in re.findall(r"[\w'-]+", names) if len(v) > 1]


def redact(value, hashes=()):
    value = text(value, 64000)
    value = re.sub(r"[\w.+-]+\s*@\s*[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", value)
    value = re.sub(r"\b(?:[\w.+-]+\s+)+(?:at|@)\s+(?:[\w.-]+\s+)*(?:dot|\.)\s*(?:com|net|org|edu|io)\b", "[EMAIL]", value, flags=re.I)
    value = re.sub(r"(?<!\w)(?:\+?\d[\d ().-]{6,}\d)(?!\w)", "[NUMBER]", value)
    value = re.sub(r"\b\d{1,6}\s+(?:[A-Za-z0-9.'-]+\s+){1,5}(?:street|st|road|rd|avenue|ave|drive|dr|lane|ln|court|ct|boulevard|blvd|way|trail|highway|hwy)\b(?:\s+(?:apt|unit|suite)\s*\w+)?", "[ADDRESS]", value, flags=re.I)
    known = set(hashes)
    value = re.sub(r"[\w'-]+", lambda m: "[NAME]" if hashlib.sha256(m[0].casefold().encode()).hexdigest() in known else m[0], value)
    value = re.sub(r"\b(my name is|this is|I'm|I am)\s+([A-Z][a-z'-]+)(?:\s+([A-Z][a-z'-]+))?", r"\1 [NAME]", value)
    return value


def dialogue(value, hashes=()):
    rows = envelope(value, list)
    if len(rows) > 5000:
        raise SourceError("response_too_large")
    output, size = [], 0
    for row in rows:
        if not isinstance(row, dict): raise SourceError("response_contract")
        role = text(row.get("role"), 100, True)
        content = text(row.get("content"), 64000)
        size += len(content)
        if size > 800000: raise SourceError("response_too_large")
        # Vendor prompts and tool payloads never enter learning or model context.
        if role not in {"agent", "assistant", "user"}: continue
        output.append({"role": "assistant" if role in {"agent", "assistant"} else "caller",
                       "text": redact(content, hashes)})
    return output


class OutreachSource:
    def __init__(self, token, transport=None):
        self.http = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + token},
            timeout=20, trust_env=False, follow_redirects=False, transport=transport)

    def close(self): self.http.close()

    def get(self, path, params=None):
        try:
            with self.http.stream("GET", path, params=params) as response:
                status = response.status_code
                if status in {401, 403}: raise SourceError("authentication" if status == 401 else "permission", 900)
                if status == 429:
                    try: delay = float(response.headers.get("Retry-After", "60"))
                    except ValueError:
                        try: delay = (parsedate_to_datetime(response.headers["Retry-After"]) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError, KeyError): delay = 60
                    raise SourceError("rate_limit", max(1, min(86400, delay)))
                if not 200 <= status < 300: raise SourceError("provider_unavailable")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > 1024 * 1024: raise SourceError("response_too_large")
                try: return json.loads(body)
                except (ValueError, UnicodeDecodeError): raise SourceError("response_contract") from None
        except httpx.HTTPError:
            raise SourceError("provider_unavailable") from None

    def identify(self, account, environment):
        row = envelope(self.get("me"), dict)
        if row.get("environment") != environment or row.get("level") != "account" or row.get("organisationId") != account or row.get("keyOrganisationId") != account:
            raise SourceError("account_mismatch", 900)
        rows = envelope(self.get("projects"), list)
        if len(rows) > 1000: raise SourceError("response_contract")
        projects = []
        for row in rows:
            if not isinstance(row, dict) or type(row.get("active")) is not bool: raise SourceError("response_contract")
            projects.append({"id": text(row.get("id"), 160, True), "name": text(row.get("name"), 240, True), "active": row["active"]})
        if len({p["id"] for p in projects}) != len(projects): raise SourceError("duplicate_project")
        return projects
