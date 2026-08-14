from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_production_pipeline_uses_full_duplex_externalmedia(
    project_root: Path,
) -> None:
    module = load_module(
        "production_externalmedia_config",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    patch = module.production_patch()
    external = patch["external_media"]

    assert patch["audio_transport"] == "externalmedia"
    assert patch["downstream_mode"] == "stream"
    assert external["direction"] == "both"
    assert external["codec"] == "ulaw"
    assert external["format"] == "slin16"
    assert external["sample_rate"] == 16000
    assert external["rtp_host"] == (
        "${FLOODMAN_EXTERNALMEDIA_RTP_HOST:-127.0.0.1}"
    )
    assert external["advertise_host"] == (
        "${FLOODMAN_EXTERNALMEDIA_ADVERTISE_HOST:-127.0.0.1}"
    )
    assert external["port_range"] == (
        "${FLOODMAN_EXTERNALMEDIA_PORT_RANGE:-18080:18099}"
    )
    assert external["allowed_remote_hosts"] == ["127.0.0.1"]
    assert external["lock_remote_endpoint"] is True


def test_production_pipeline_components_are_preserved(
    project_root: Path,
) -> None:
    module = load_module(
        "production_externalmedia_components",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    pipeline = module.production_patch()["pipelines"][
        "floodman_production"
    ]
    assert pipeline["stt"] == "deepgram_flux_stt"
    assert pipeline["llm"] == "groq_llm"
    assert pipeline["tts"] == "elevenlabs_tts"


def test_local_fallback_keeps_audiosocket(
    project_root: Path,
) -> None:
    module = load_module(
        "local_audiosocket_config",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    patch = module.local_patch()
    assert patch["audio_transport"] == "audiosocket"
    assert patch["downstream_mode"] == "stream"
    assert patch["audiosocket"]["host"] == "127.0.0.1"
    assert patch["audiosocket"]["port"] == 8090
    assert patch["audiosocket"]["format"] == "slin"


def test_internal_ports_do_not_overlap_twilio_rtp(
    project_root: Path,
) -> None:
    module = load_module(
        "production_externalmedia_ports",
        project_root / "scripts/apply_production_hybrid_ai.py",
    )
    port_range = module.production_patch()["external_media"][
        "port_range"
    ]
    assert "18080:18099" in port_range
    assert "10000:10040" not in port_range
