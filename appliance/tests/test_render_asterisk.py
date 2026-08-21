import importlib.util
from pathlib import Path

def test_asterisk_renderer(project_root: Path,tmp_path,monkeypatch):
    monkeypatch.setenv("DATA_DIR",str(tmp_path))
    monkeypatch.setenv("SIP_MODE","generic")
    monkeypatch.setenv("SIP_SERVER","carrier.example.com")
    monkeypatch.setenv("SIP_MATCH_ADDRESSES","192.0.2.10/32")
    path=project_root/"scripts/render_asterisk.py";spec=importlib.util.spec_from_file_location("render_test",path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    assert module.main()==0
    extensions=(tmp_path/"asterisk/etc/extensions.conf").read_text()
    modules=(tmp_path/"asterisk/etc/modules.conf").read_text()
    assert "AudioSocket" in extensions
    assert "agi_prepare.py" in extensions and "agi_finish.py" in extensions
    assert "FLOODMAN_DID=${EXTEN}" in extensions
    assert "noload => res_odbc.so" in modules
    assert (tmp_path/"asterisk/etc/manager.conf").read_text().strip().endswith("enabled=no")
