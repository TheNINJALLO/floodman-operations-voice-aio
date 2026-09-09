from pathlib import Path


def test_unified_image_runs_voice_and_business_suite_on_distinct_ports(project_root: Path):
    dockerfile = (project_root / "unified" / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (project_root / "unified" / "entrypoint.sh").read_text(encoding="utf-8")
    supervisor = (project_root / "unified" / "voice-supervisor.conf").read_text(encoding="utf-8")

    assert "floodman-operations:4.7.2@sha256:" in dockerfile
    assert "nvidia/cuda:12.4.1-runtime-ubuntu22.04@sha256:" in dockerfile
    assert "8002/tcp 9000/tcp 9001/tcp 9002/tcp 9003/tcp 9004/tcp" in dockerfile
    assert "AUDIOSOCKET_PORT=8091" in entrypoint
    assert "WEB_PORT=8802" in entrypoint
    assert "BUSINESS_SUITE_EVENTS_URL=\"http://127.0.0.1:9004/" in entrypoint
    assert "export VIRTUAL_ENV=/opt/voice-venv" in entrypoint
    assert "readonly PYTHON_BIN=/opt/voice-venv/bin/python" in entrypoint
    assert "/opt/python312/bin" in entrypoint
    assert 'VIRTUAL_ENV="${VIRTUAL_ENV:-/opt/voice-venv}"' not in entrypoint
    assert "cp -R --no-preserve=mode,ownership,timestamps /opt/floodman/hub/." in entrypoint
    assert "cp -a /opt/floodman/hub/." not in entrypoint
    assert 'export APP_LOGO="${APP_LOGO:-${FLOODMAN_PUBLIC_URL}/floodman-brand/floodman-wordmark.svg}"' in entrypoint
    assert "PUBLIC_BASE_URL=\"${VOICE_PUBLIC_BASE_URL" in (project_root / "unified" / "start-voice-control.sh").read_text(encoding="utf-8")
    assert "program:voice-llama" in supervisor
    assert "program:voice-control" in supervisor
    assert "program:voice-asterisk" in supervisor
    assert "program:unified-public-gateway" in supervisor

    gateway = (project_root / "unified" / "gateway-nginx.conf").read_text(encoding="utf-8")
    assert "server_name aicall.oninetwork.com" in gateway
    assert "proxy_pass http://127.0.0.1:8802" in gateway
    assert "server_name floodman.oninetwork.com" in gateway
    assert "proxy_pass http://127.0.0.1:9000" in gateway
    assert "server_name sign.oninetwork.com" in gateway
    assert "server_name lab.oninetwork.com" in gateway
    assert "server_name api.oninetwork.com" in gateway


def test_unified_mutable_paths_resolve_under_data_dir(project_root: Path):
    dockerfile = (project_root / "unified" / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (project_root / "unified" / "entrypoint.sh").read_text(encoding="utf-8")

    for name in ("config", "run", "logs", "runtime", "backups", "diagnostics", "tmp"):
        assert f"ln -s data/business/{name} /home/container/{name}" in dockerfile
    assert "ln -s /home/container/data/business/gauzy-files /srv/gauzy/apps/api/public" in dockerfile
    assert "ln -s /home/container/data/business/gauzy-import /import" in dockerfile
    assert 'FM_DATA="${DATA_DIR}/business"' in entrypoint
    assert 'owner_file="${FM_CONFIG}/unified-owner.env"' in entrypoint
    assert 'for business_path in config run logs runtime backups diagnostics tmp' in entrypoint
    assert 'ln -s "data/business/${business_path}" "${compatibility_path}"' in entrypoint
    assert "Refusing to replace unexpected persistent path" in entrypoint
    assert "BUSINESS_RUNTIME_SHA256=f8d3d07ff885ecde57703abba1567d151ecc4419c5afb4a73bcb5f91a054f177" in dockerfile
    assert 'sha256sum -c -' in dockerfile
    assert "PYTHON_VERSION=3.12.11" in dockerfile
    assert "PYTHON_SOURCE_SHA256=c30bb24b7f1e9a19b11b55a546434f74e739bb4c271a3e3a80ff4380d49f7adb" in dockerfile
    assert "COPY --from=python-build /opt/python312/ /opt/python312/" in dockerfile
    assert 'unzip -q "${business_runtime_zip}" -d "${business_overlay_next}"' in entrypoint
    assert 'sha256sum -c MANIFEST.sha256' in entrypoint
    assert 'prepare-roomflow.py' in entrypoint
    assert '${DATA_DIR}/roomflow/current/index.html' in entrypoint


def test_unified_health_requires_both_product_surfaces(project_root: Path):
    health = (project_root / "unified" / "healthcheck.sh").read_text(encoding="utf-8")
    assert "127.0.0.1:8002/ready" in health
    assert "127.0.0.1:9000/health/live" in health
    assert "127.0.0.1:9004/health/ready" in health
