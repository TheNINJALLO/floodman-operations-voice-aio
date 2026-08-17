#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


MARKER = "Floodman direct tool continuation patch"


class PatchError(RuntimeError):
    pass


HELPER = '\n\n# Floodman direct tool continuation patch.\ndef _floodman_direct_tool_payload(\n    result,\n) -> tuple[str, bool]:\n    """Return a reviewed tool sentence without another LLM rewrite."""\n\n    if not isinstance(result, dict):\n        return "", False\n\n    payload = result.get("data")\n    if not isinstance(payload, dict):\n        payload = result\n\n    if not bool(payload.get("speak_verbatim")):\n        return "", False\n\n    message = str(\n        payload.get("safe_message")\n        or payload.get("next_question")\n        or ""\n    ).strip()\n    end_call = bool(\n        payload.get("end_call_after_message")\n    )\n    return message, end_call\n'


def _direct_branch(indent: str) -> str:
    one = indent + "    "
    two = one + "    "
    three = two + "    "
    four = three + "    "
    return (
        f"{indent}direct_tool_message, direct_tool_end_call = (\n"
        f"{one}_floodman_direct_tool_payload(result)\n"
        f"{indent})\n"
        f"{indent}if direct_tool_message:\n"
        f"{one}conversation_history.append(\n"
        f"{two}_ts_msg(\"assistant\", direct_tool_message)\n"
        f"{one})\n"
        f"{one}session.conversation_history = list(\n"
        f"{two}conversation_history\n"
        f"{one})\n"
        f"{one}await self.session_store.upsert_call(session)\n"
        f"{one}logger.info(\n"
        f"{two}\"Direct tool continuation response\",\n"
        f"{two}tool=name,\n"
        f"{two}call_id=call_id,\n"
        f"{two}preview=direct_tool_message[:80],\n"
        f"{one})\n"
        f"{one}if not self._pipeline_output_allowed(\n"
        f"{two}call_id,\n"
        f"{two}session,\n"
        f"{two}stage=\"direct-tool-tts\",\n"
        f"{one}):\n"
        f"{two}return\n"
        f"{one}if self._pipeline_tts_uses_streaming(pipeline):\n"
        f"{two}try:\n"
        f"{three}await self._stream_pipeline_tts_text(\n"
        f"{four}call_id,\n"
        f"{four}session,\n"
        f"{four}pipeline,\n"
        f"{four}direct_tool_message,\n"
        f"{three})\n"
        f"{two}except _PipelinePlaybackInterrupted:\n"
        f"{three}logger.info(\n"
        f"{four}\"Direct tool response interrupted\",\n"
        f"{four}call_id=call_id,\n"
        f"{three})\n"
        f"{three}return\n"
        f"{one}else:\n"
        f"{two}direct_bytes = bytearray()\n"
        f"{two}async for chunk in pipeline.tts_adapter.synthesize(\n"
        f"{three}call_id,\n"
        f"{three}direct_tool_message,\n"
        f"{three}pipeline.tts_options,\n"
        f"{two}):\n"
        f"{three}if chunk:\n"
        f"{four}direct_bytes.extend(chunk)\n"
        f"{two}if direct_bytes:\n"
        f"{three}direct_pid = await self.playback_manager.play_audio(\n"
        f"{four}call_id,\n"
        f"{four}bytes(direct_bytes),\n"
        f"{four}\"pipeline-direct-tool\",\n"
        f"{three})\n"
        f"{three}if direct_pid:\n"
        f"{four}await self.playback_manager.wait_for_playback_end(\n"
        f"{four}    call_id,\n"
        f"{four}    direct_pid,\n"
        f"{four}    timeout_sec=(\n"
        f"{four}        len(direct_bytes) / 8000.0 + 3.0\n"
        f"{four}    ),\n"
        f"{four})\n"
        f"{one}if direct_tool_end_call:\n"
        f"{two}await self._terminate_call_after_audio(\n"
        f"{three}call_id,\n"
        f"{three}reason=\"completed_intake\",\n"
        f"{three}call_outcome=\"agent_hangup\",\n"
        f"{three}audio_already_drained=True,\n"
        f"{two})\n"
        f"{two}return\n"
        f"{one}continue\n"
    )


def patch_file(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        return False

    import_anchor = "from src.pipelines.base import LLMResponse\n"
    if text.count(import_anchor) != 1:
        raise PatchError(
            "LLMResponse import anchor was not found exactly once"
        )
    text = text.replace(
        import_anchor,
        import_anchor + HELPER,
        1,
    )

    pattern = re.compile(
        r'^(?P<indent>[ \t]+)'
        r'tool_result_msg = result\.get\('
        r'"message", f"Tool \{name\} executed successfully\."\)'
        r'\n',
        re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        raise PatchError(
            "No non-terminal tool continuation block was found"
        )

    def replacement(match: re.Match[str]) -> str:
        return (
            _direct_branch(match.group("indent"))
            + match.group(0)
        )

    text = pattern.sub(replacement, text)
    compile(text, str(path), "exec")
    path.write_text(text, encoding="utf-8", newline="\n")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ava-root",
        type=Path,
        default=Path("/opt/ava"),
    )
    args = parser.parse_args()

    path = args.ava_root.resolve() / "src/engine.py"
    if not path.is_file():
        raise PatchError(f"required AVA source is missing: {path}")

    changed = patch_file(path)
    print(
        f"{'patched' if changed else 'already patched'} {path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
