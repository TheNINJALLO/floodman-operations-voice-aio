from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPRESENTATIVE_ENGINE = r"""
from src.pipelines.base import LLMResponse


class Engine:
    async def run(self, result, pipeline, session, call_id):
        conversation_history = []
        for tool_call in [{}]:
            name = "floodman_capture_intake_progress"
            canonical_tool = name
            if not result.get("will_hangup") and canonical_tool not in ("blind_transfer", "live_agent_transfer"):
                tool_result_msg = result.get("message", f"Tool {name} executed successfully.")
                context_for_llm = {}
                llm_response = await pipeline.llm_adapter.generate(
                    call_id,
                    "",
                    context_for_llm,
                    pipeline.llm_options,
                )
"""


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_direct_tool_patch_bypasses_second_llm(
    project_root: Path,
    tmp_path: Path,
) -> None:
    patcher = _load(
        "direct_tool_patch_test",
        project_root
        / "scripts/patch_ava_direct_tool_prompts.py",
    )
    target = tmp_path / "engine.py"
    target.write_text(
        REPRESENTATIVE_ENGINE,
        encoding="utf-8",
    )

    assert patcher.patch_file(target) is True
    assert patcher.patch_file(target) is False

    source = target.read_text(encoding="utf-8")
    compile(source, str(target), "exec")
    assert "Floodman direct tool continuation patch" in source
    assert "Direct tool continuation response" in source
    assert "end_call_after_message" in source

    direct_index = source.index(
        "_floodman_direct_tool_payload(result)"
    )
    fallback_index = source.index(
        "tool_result_msg = result.get"
    )
    assert direct_index < fallback_index


def test_container_applies_direct_tool_patch(
    project_root: Path,
) -> None:
    dockerfile = (
        project_root / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert (
        "COPY scripts/patch_ava_direct_tool_prompts.py "
        "/opt/floodman-build/patch_ava_direct_tool_prompts.py"
        in dockerfile
    )
    assert (
        "patch_ava_direct_tool_prompts.py --ava-root /opt/ava"
        in dockerfile
    )
    assert (
        'grep -q "Floodman direct tool continuation patch"'
        in dockerfile
    )
