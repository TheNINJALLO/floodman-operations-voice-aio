#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def read_agi_environment() -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in sys.stdin:
        line = raw.rstrip("\r\n")
        if not line:
            break
        if ":" in line:
            key, value = line.split(":", 1)
            env[key.strip()] = value.strip()
    return env


def agi_command(command: str) -> str:
    print(command, flush=True)
    return sys.stdin.readline().rstrip("\r\n")


def get_variable(name: str) -> str:
    response = agi_command(f"GET VARIABLE {name}")
    marker = "result=1 ("
    if marker in response and response.endswith(")"):
        return response.split(marker, 1)[1][:-1]
    return ""


def set_variable(name: str, value: Any) -> None:
    text = str(value if value is not None else "").replace("\r", " ").replace("\n", " ")
    text = text.replace('"', "'")[:2048]
    agi_command(f'SET VARIABLE {name} "{text}"')


def internal_url() -> str:
    return os.getenv("FLOODMAN_INTERNAL_URL", "http://127.0.0.1:9000").rstrip("/")


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"{internal_url()}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": os.getenv("INTERNAL_TOKEN", ""),
        },
    )
    with urllib.request.urlopen(request, timeout=float(os.getenv("AGI_HTTP_TIMEOUT", "3"))) as response:
        return json.loads(response.read().decode("utf-8"))
