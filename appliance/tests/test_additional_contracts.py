from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.business import BusinessDirectory
from app.config import Settings
from app.models import IntakeState
from app.notifications import build_message
from app.tts import LocalTTS


def load_envfile(project_root: Path):
    path = project_root / "scripts/envfile.py"
    spec = importlib.util.spec_from_file_location("envfile_extra", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_blank_parent_environment_does_not_hide_generated_secret(project_root: Path, tmp_path: Path, monkeypatch):
    module = load_envfile(project_root)
    path = tmp_path / "runtime.env"
    path.write_text("export ADMIN_TOKEN=generated-value\n", encoding="utf-8")
    monkeypatch.setenv("ADMIN_TOKEN", "")
    output = module.shell_exports(path, missing_only=True)
    assert "generated-value" in output


def test_company_answers_are_deterministic(project_root: Path):
    business = BusinessDirectory(project_root / "config/service_area.yaml")
    assert "water-damage restoration" in business.direct_answer("What services do you offer?")
    assert "published service area" in business.direct_answer("Do you serve Grand Rapids?")
    assert "not a service" in business.direct_answer("Do you repair roofs?")


def test_sms_contains_every_recovered_field():
    state = IntakeState(
        call_uuid="call-x",
        name="Josh Aldrich",
        phone="+12318840943",
        email="josh@example.com",
        address="1 Main Street",
        service_key="water_damage_restoration",
        service_status="supported",
        description="Water in basement",
        property_context="Residential property",
        timing_summary="This morning",
        safety_summary="No immediate concern",
        service_area_status="published",
        service_area_city="Ludington",
    )
    body = build_message(state, 24, partial=False)
    for value in ("Josh Aldrich", "+12318840943", "josh@example.com", "1 Main Street", "Water in basement", "24 hours"):
        assert value in body


def test_trusted_host_is_derived_from_public_url(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://voice.example.com")
    monkeypatch.delenv("TRUSTED_HOSTS", raising=False)
    settings = Settings.from_env()
    assert "voice.example.com" in settings.trusted_hosts


def test_tts_cache_key_is_stable(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    settings = Settings.from_env()
    tts = LocalTTS(settings)
    assert tts._cache_path("hello") == tts._cache_path("hello")
    assert tts._cache_path("hello") != tts._cache_path("goodbye")


def test_ari_and_ami_are_not_exposed(project_root: Path):
    renderer = (project_root / "scripts/render_asterisk.py").read_text(encoding="utf-8")
    assert 'write(etc, "http.conf", "[general]\\nenabled=no")' in renderer
    assert 'write(etc, "manager.conf", "[general]\\nenabled=no")' in renderer


def test_model_downloads_are_persistent(project_root: Path):
    source = (project_root / "scripts/download_models.py").read_text(encoding="utf-8")
    assert 'models / "llm"' in source
    assert 'models / "kokoro"' in source
    assert "snapshot_download" in source
