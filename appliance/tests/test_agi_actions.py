import importlib.util
import io
import json
import sys
from pathlib import Path
import pytest


def load_script(project_root: Path, name: str, monkeypatch):
    path = project_root / "scripts" / name
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_agi(module, monkeypatch, argv: list[str]) -> str:
    stdin = io.StringIO("agi_request: test\n\n" + "200 result=1\n" * 3)
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert module.main() == 0
    return stdout.getvalue()


def test_prepare_persists_call_identifiers(project_root: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    module = load_script(project_root, "agi_prepare.py", monkeypatch)
    output = run_agi(
        module,
        monkeypatch,
        ["agi_prepare.py", "test-uuid", "+12315550100", "+12318668376", "171.42", "sip-call-id@example"],
    )
    payload = json.loads((tmp_path / "precall" / "test-uuid.json").read_text(encoding="utf-8"))
    assert output == 'SET VARIABLE FLOODMAN_PREPARED "1"\n'
    assert payload["asterisk_channel_id"] == "171.42"
    assert payload["sip_call_id"] == "sip-call-id@example"


def test_finish_distinguishes_missing_action(project_root: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    module = load_script(project_root, "agi_finish.py", monkeypatch)
    output = run_agi(module, monkeypatch, ["agi_finish.py", "test-uuid"])
    assert 'FLOODMAN_ACTION "missing_action"' in output
    assert 'FLOODMAN_ACTION_REASON "no_action_file"' in output


def test_finish_returns_explicit_action(project_root: Path, tmp_path: Path, monkeypatch) -> None:
    actions = tmp_path / "actions"
    actions.mkdir()
    path = actions / "test-uuid.json"
    path.write_text(
        json.dumps({"action": "completed", "number": "", "reason": "intake_complete"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    module = load_script(project_root, "agi_finish.py", monkeypatch)
    output = run_agi(module, monkeypatch, ["agi_finish.py", "test-uuid"])
    assert 'FLOODMAN_ACTION "completed"' in output
    assert 'FLOODMAN_ACTION_REASON "intake_complete"' in output
    assert not path.exists()


def test_finish_applies_and_acknowledges_all_transfer_variables(project_root, tmp_path, monkeypatch):
    actions = tmp_path / "actions"
    actions.mkdir()
    path = actions / "transfer.json"
    path.write_text(json.dumps({"action": "transfer", "number": "+12318840943", "reason": "assistant_transfer"}))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    module = load_script(project_root, "agi_finish.py", monkeypatch)
    output = run_agi(module, monkeypatch, ["agi_finish.py", "transfer"])
    assert output.splitlines() == ['SET VARIABLE FLOODMAN_ACTION "transfer"',
                                   'SET VARIABLE FLOODMAN_TRANSFER_NUMBER "+12318840943"',
                                   'SET VARIABLE FLOODMAN_ACTION_REASON "assistant_transfer"']
    assert not path.exists()


@pytest.mark.parametrize("response", ["", "200 result=-1\n", "510 Invalid or unknown command\n"])
def test_unacknowledged_agi_command_retains_the_transfer_action(project_root, tmp_path, monkeypatch, response):
    actions = tmp_path / "actions"
    actions.mkdir()
    path = actions / "transfer.json"
    path.write_text(json.dumps({"action": "transfer", "number": "+12318840943"}))
    monkeypatch.setenv("RUNTIME_DIR", str(tmp_path))
    module = load_script(project_root, "agi_finish.py", monkeypatch)
    monkeypatch.setattr(sys, "argv", ["agi_finish.py", "transfer"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" + response))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    with pytest.raises(RuntimeError, match="did not acknowledge"):
        module.main()
    assert path.exists() and len(output.getvalue().splitlines()) == 1
