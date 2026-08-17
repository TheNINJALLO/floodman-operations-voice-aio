from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from app.notifications import build_intake_sms, team_alert_recipients


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_team_recipient_routing_deduplicates_and_uses_department() -> None:
    settings = SimpleNamespace(
        team_alert_numbers=("+12315550001",),
        estimating_alert_numbers=("+12315550001", "+12315550002"),
        emergency_alert_numbers=("+12315550003",),
        billing_alert_numbers=(),
        support_alert_numbers=(),
    )
    assert team_alert_recipients(settings, "estimating") == (
        "+12315550001",
        "+12315550002",
    )
    assert team_alert_recipients(settings, "emergency") == (
        "+12315550001",
        "+12315550003",
    )


def test_team_sms_contains_required_intake_fields() -> None:
    body = build_intake_sms(
        {
            "name": "Josh Aldrich",
            "phone": "+12315550199",
            "address": "123 Main Street, Traverse City, MI",
            "description": "Basement wall is leaking after rain",
            "service": "waterproofing",
            "department": "estimating",
            "urgency": "normal",
            "call_id": "call-123",
        },
        24,
    )
    assert "Josh Aldrich" in body
    assert "+12315550199" in body
    assert "123 Main Street" in body
    assert "Basement wall is leaking" in body
    assert "within 24 hours" in body


def test_inbound_agent_never_books_and_uses_submit_intake(project_root: Path) -> None:
    source = (project_root / "app/ava/agents.py").read_text(encoding="utf-8")
    inbound = source.split('slug="floodman_inbound"', 1)[1].split(
        'slug="floodman_google_business"', 1
    )[0]
    assert "floodman_submit_intake" in inbound
    assert "never books" in inbound
    assert "within 24 hours" in inbound
    assert "floodman_schedule_inspection" not in inbound
    assert "floodman_check_availability" not in inbound


def test_scheduling_tools_are_disabled_and_submit_tool_is_fast(project_root: Path) -> None:
    config = yaml.safe_load(
        (project_root / "config/ava/ai-agent.local.yaml").read_text(encoding="utf-8")
    )
    tools = config["in_call_tools"]
    assert tools["floodman_submit_intake"]["enabled"] is True
    assert tools["floodman_submit_intake"]["timeout_ms"] <= 2500
    for name in (
        "floodman_check_availability",
        "floodman_schedule_inspection",
        "floodman_reschedule_inspection",
    ):
        assert tools[name]["enabled"] is False
        assert tools[name]["is_global"] is False


def test_production_latency_defaults_are_reduced(project_root: Path) -> None:
    module = load_module(
        "fast_intake_profile",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    patch = module.production_patch()
    stt = patch["pipelines"]["floodman_production"]["options"]["stt"]
    llm = patch["pipelines"]["floodman_production"]["options"]["llm"]
    assert stt["eot_timeout_ms"] == "${DEEPGRAM_EOT_TIMEOUT_MS:-1500}"
    assert llm["max_tokens"] == "${GROQ_LLM_MAX_TOKENS:-160}"
    assert llm["rate_limit_retries"] == (
        "${GROQ_RATE_LIMIT_RETRIES:-1}"
    )
    assert llm["rate_limit_max_wait_sec"] == (
        "${GROQ_RATE_LIMIT_MAX_WAIT_SECONDS:-3}"
    )
    assert patch["streaming"]["jitter_buffer_ms"] == (
        "${FLOODMAN_STREAMING_JITTER_BUFFER_MS:-80}"
    )
    assert patch["profiles"]["telephony_enhanced_8k"]["idle_cutoff_ms"] == (
        "${FLOODMAN_TTS_IDLE_CUTOFF_MS:-220}"
    )


def test_submit_intake_queues_sms_instead_of_waiting_on_roomflow(
    project_root: Path,
) -> None:
    source = (project_root / "app/business/service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_op_submit_intake"
    )

    queued_operations: set[str] = set()
    awaited_sync_calls: list[ast.Await] = []
    for node in ast.walk(method):
        if isinstance(node, ast.Call):
            function = node.func
            if (
                isinstance(function, ast.Attribute)
                and function.attr == "queue_outbox"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                queued_operations.add(node.args[0].value)
        elif isinstance(node, ast.Await):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "_sync"
            ):
                awaited_sync_calls.append(node)

    method_source = ast.get_source_segment(source, method) or ""
    assert "team_sms_alert" in queued_operations
    assert "create_lead" in queued_operations
    assert not awaited_sync_calls
    assert "will call you within" in method_source.lower()


def test_invalid_legacy_workflow_is_removed(project_root: Path) -> None:
    assert not (project_root / ".github/workflows/repair-cpu-audiosocket.yml").exists()


def test_fast_intake_greeting_is_consistent_and_disclosed(
    project_root: Path,
) -> None:
    greeting = (
        "Thanks for calling Floodman. This is Ava, the automated "
        "assistant. How can I help?"
    )
    agents = (project_root / "app/ava/agents.py").read_text(
        encoding="utf-8"
    )
    overlay = yaml.safe_load(
        (project_root / "config/ava/ai-agent.local.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert greeting in agents
    assert overlay["llm"]["initial_greeting"] == greeting
    assert "automated assistant" in greeting.lower()

