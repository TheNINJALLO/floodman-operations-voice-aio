from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, project_root: Path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CONFIG_DIR", str(project_root / "config"))
    monkeypatch.setenv("WEB_DIR", str(project_root / "web"))
    monkeypatch.setenv("GATE_ENABLED", "false")
    monkeypatch.setenv("OUTBOUND_ENABLED", "false")
    monkeypatch.setenv("AMI_ENABLED", "false")
    monkeypatch.setenv("ROOMFLOW_ENABLED", "false")
    monkeypatch.setenv("RECONCILE_AVA_AGENTS", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-test-token")
    monkeypatch.setenv("INTERNAL_TOKEN", "internal-test-token")
    monkeypatch.setenv("UPLOAD_TOKEN_SECRET", "upload-test-secret-with-enough-entropy")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://testserver")
    from app.config import Settings

    return Settings.from_env(project_root)
