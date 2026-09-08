import importlib.util
from pathlib import Path


def test_asterisk_renderer(
    project_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_dir = tmp_path / "asterisk-modules"
    module_dir.mkdir()
    (module_dir / "app_audiosocket.so").touch()

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SIP_MODE", "generic")
    monkeypatch.setenv(
        "SIP_SERVER",
        "carrier.example.com",
    )
    monkeypatch.setenv(
        "SIP_MATCH_ADDRESSES",
        "192.0.2.10/32",
    )
    monkeypatch.setenv(
        "ASTERISK_MODULE_DIR",
        str(module_dir),
    )

    path = project_root / "scripts/render_asterisk.py"
    spec = importlib.util.spec_from_file_location(
        "render_test",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main() == 0

    etc = tmp_path / "asterisk/etc"
    asterisk = (etc / "asterisk.conf").read_text()
    extensions = (etc / "extensions.conf").read_text()
    logger = (etc / "logger.conf").read_text()
    modules = (etc / "modules.conf").read_text()

    assert f"astmoddir => {module_dir}" in asterisk
    assert "TryExec(AudioSocket" in extensions
    assert "Set(__FLOODMAN_CALL_ID=${UUID()})" in extensions
    assert "SHELL(cat /proc/sys/kernel/random/uuid)" not in extensions
    assert "agi_prepare.py" in extensions
    assert "agi_finish.py" in extensions
    assert "FLOODMAN_ACTION=missing_action" in extensions
    assert "Playback(floodman-technical-failure)" in extensions
    assert "stage=audiosocket_return" in extensions
    assert "stage=hangup" in extensions
    assert f"{tmp_path / 'logs' / 'asterisk-full.log'} => notice,warning,error" in logger
    assert "full =>" not in logger
    assert "FLOODMAN_DID=${EXTEN}" in extensions
    assert "noload => res_odbc.so" in modules
    assert (
        (etc / "manager.conf")
        .read_text()
        .strip()
        .endswith("enabled=no")
    )
