from pathlib import Path


def test_unified_image_runs_voice_and_business_suite_on_distinct_ports(project_root: Path):
    dockerfile = (project_root / "unified" / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (project_root / "unified" / "entrypoint.sh").read_text(encoding="utf-8")
    supervisor = (project_root / "unified" / "voice-supervisor.conf").read_text(encoding="utf-8")

    assert "floodman-operations:4.7.2@sha256:" in dockerfile
    assert "nvidia/cuda:12.4.1-runtime-ubuntu22.04@sha256:" in dockerfile
    assert "8002/tcp 9000/tcp 9001/tcp 9002/tcp 9003/tcp 9004/tcp" in dockerfile
    assert "AUDIOSOCKET_PORT=8091" in entrypoint
    assert "BUSINESS_SUITE_EVENTS_URL=\"http://127.0.0.1:9004/" in entrypoint
    assert "PUBLIC_BASE_URL=\"${VOICE_PUBLIC_BASE_URL" in (project_root / "unified" / "start-voice-control.sh").read_text(encoding="utf-8")
    assert "program:voice-llama" in supervisor
    assert "program:voice-control" in supervisor
    assert "program:voice-asterisk" in supervisor


def test_unified_mutable_paths_resolve_under_data_dir(project_root: Path):
    dockerfile = (project_root / "unified" / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (project_root / "unified" / "entrypoint.sh").read_text(encoding="utf-8")

    for name in ("config", "run", "logs", "runtime", "backups", "diagnostics", "tmp"):
        assert f"ln -s data/business/{name} /home/container/{name}" in dockerfile
    assert 'FM_DATA="${DATA_DIR}/business"' in entrypoint
    assert 'owner_file="${FM_CONFIG}/unified-owner.env"' in entrypoint


def test_unified_health_requires_both_product_surfaces(project_root: Path):
    health = (project_root / "unified" / "healthcheck.sh").read_text(encoding="utf-8")
    assert "127.0.0.1:8002/ready" in health
    assert "127.0.0.1:9000/health/live" in health
    assert "127.0.0.1:9004/health/ready" in health

