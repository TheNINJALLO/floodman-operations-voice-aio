from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_ava_patch_json_escapes_in_call_and_pre_call_bodies(project_root, tmp_path):
    ava_root = tmp_path / "ava"
    http_dir = ava_root / "src/tools/http"
    http_dir.mkdir(parents=True)

    in_call = http_dir / "in_call_lookup.py"
    in_call.write_text(
        '''import json\nimport os\nimport re\nfrom typing import Dict\n\nclass InCallHTTPTool:\n    def build(self, sub_context):\n        body_str = self._substitute_variables(self.config.body_template, sub_context)\n        return body_str\n\n    def _substitute_variables(self, template: str, context: Dict[str, str]) -> str:\n        result = template\n        for key, value in context.items():\n            result = result.replace(f"{{{key}}}", value)\n        return result\n''',
        encoding="utf-8",
    )

    pre_call = http_dir / "generic_lookup.py"
    pre_call.write_text(
        '''import json\nimport os\nimport re\n\nclass PreCallContext:\n    def __init__(self, value):\n        self.caller_number = value\n        self.called_number = ""\n        self.caller_name = value\n        self.context_name = ""\n        self.call_id = "call-1"\n        self.campaign_id = ""\n        self.lead_id = ""\n\nclass GenericHTTPLookupTool:\n    def build(self, context):\n        body = self._substitute_variables(self.config.body_template, context)\n        return body\n\n    def _substitute_variables(self, template: str, context: PreCallContext) -> str:\n        return template.replace("{caller_name}", context.caller_name or "")\n''',
        encoding="utf-8",
    )

    command = [
        sys.executable,
        str(project_root / "scripts/patch_ava.py"),
        "--ava-root",
        str(ava_root),
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    assert "patched" in first.stdout

    special = 'Kaitlyn "Flood"\\Office\nBasement\tEast'
    in_module = _load_module(in_call, "patched_in_call_lookup")
    in_tool = in_module.InCallHTTPTool()
    rendered = in_tool._substitute_json_variables(
        '{"name":"{name}","notes":"{notes}"}',
        {"name": special, "notes": special},
    )
    assert json.loads(rendered) == {"name": special, "notes": special}

    pre_module = _load_module(pre_call, "patched_generic_lookup")
    pre_tool = pre_module.GenericHTTPLookupTool()
    context = pre_module.PreCallContext(special)
    rendered = pre_tool._substitute_json_variables(
        '{"caller":"{caller_name}","phone":"{caller_number}"}', context
    )
    assert json.loads(rendered) == {"caller": special, "phone": special}

    second = subprocess.run(command, check=True, capture_output=True, text=True)
    assert "already applied" in second.stdout


def test_ava_patch_fails_closed_when_upstream_source_changes(project_root, tmp_path):
    ava_root = tmp_path / "ava"
    http_dir = ava_root / "src/tools/http"
    http_dir.mkdir(parents=True)
    (http_dir / "in_call_lookup.py").write_text("# unexpected upstream layout\n")
    (http_dir / "generic_lookup.py").write_text("# unexpected upstream layout\n")

    result = subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/patch_ava.py"),
            "--ava-root",
            str(ava_root),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "expected exactly one source match" in result.stderr
