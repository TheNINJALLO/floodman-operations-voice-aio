from __future__ import annotations

import ast
from pathlib import Path


def test_groq_preflight_uses_supported_http_client_signature(
    project_root: Path,
) -> None:
    source = (
        project_root / "scripts/validate_production_ai.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "json_request"
    )
    segment = ast.get_source_segment(source, function)
    assert segment is not None
    assert "httpx.Client" in segment
    assert "Asterisk-AI-Voice-Agent/1.0" in segment
    assert "follow_redirects=True" in segment
    assert "cf-ray" in segment
    assert "urllib.request.urlopen" not in segment


def test_groq_model_remains_current(
    project_root: Path,
) -> None:
    source = (
        project_root / "scripts/validate_production_ai.py"
    ).read_text(encoding="utf-8")
    assert "qwen/qwen3.6-27b" in source
