from pathlib import Path
import re

def test_docker_exports_venv_python(project_root: Path):
    text = (project_root / "Dockerfile").read_text()
    assert "VIRTUAL_ENV=/opt/venv" in text
    assert "PATH=/opt/venv/bin:" in text
    assert "/opt/venv/bin/python --version" in text
    assert "test -x /opt/venv/bin/python" in text

def test_entrypoint_uses_venv_python(project_root: Path):
    text = (project_root / "scripts/entrypoint.sh").read_text()
    assert 'PYTHON_BIN="${PYTHON_BIN:-${VIRTUAL_ENV}/bin/python}"' in text
    assert "Floodman Python runtime:" in text
    assert not re.search(r"(?m)^\s*python\s+/opt/floodman/", text)

def test_supervisor_uses_venv(project_root: Path):
    text = (project_root / "supervisor/supervisord.conf").read_text()
    assert "/opt/venv/bin/uvicorn" in text
