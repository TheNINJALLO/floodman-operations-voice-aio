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
    assert "/opt/floodman/unified/bin:/opt/node24/bin" in entrypoint
    assert "unbounded process tree" not in entrypoint
    assert "share_panel_directory()" in entrypoint
    assert 'setpriv --reuid="${owner}" --regid="${group}"' in entrypoint
    assert '"${FM_DATA}/gauzy-files"' in entrypoint
    assert '"${FM_DATA}/documenso"' in entrypoint
    assert '"${FM_DATA}/office"' in entrypoint
    assert 'share_panel_directory "${FM_CONFIG}"' not in entrypoint
    assert 'share_panel_directory "${FM_DATA}/postgres"' not in entrypoint

    init_wrapper = (project_root / "unified" / "postgres-init-wrapper.sh").read_text(encoding="utf-8")
    server_wrapper = (project_root / "unified" / "postgres-server-wrapper.sh").read_text(encoding="utf-8")
    pg_patch = (project_root / "unified" / "postgresql-pterodactyl-rootless.patch").read_text(encoding="utf-8")
    assert "NoNewPrivs:[[:space:]]*1" in init_wrapper
    assert "NoNewPrivs:[[:space:]]*1" in server_wrapper
    assert "P_SERVER_UUID" in init_wrapper
    assert "FLOODMAN_PTERODACTYL_ROOTLESS=1" in init_wrapper
    assert "FLOODMAN_PTERODACTYL_ROOTLESS=1" in server_wrapper
    assert "/opt/floodman/postgresql14-panel/bin/initdb" in init_wrapper
    assert "/opt/floodman/postgresql14-panel/bin/postgres" in server_wrapper
    assert pg_patch.count('getenv("FLOODMAN_PTERODACTYL_ROOTLESS") == NULL') == 2

    assert "ARG POSTGRESQL_VERSION=14.18" in dockerfile
    assert "83ab29d6bfc3dc58b2ed3c664114fdfbeb6a0450c4b8d7fa69aee91e3ca14f8e" in dockerfile
    assert "postgresql-pterodactyl-rootless.patch" in dockerfile
    assert 'make -C contrib/pgcrypto -j"$(nproc)"' in dockerfile
    assert "make -C contrib/pgcrypto install" in dockerfile
    assert 'make -C contrib/pg_trgm -j"$(nproc)"' in dockerfile
    assert "make -C contrib/pg_trgm install" in dockerfile
    assert "npm_config_build_from_source=true" in dockerfile
    assert "npm rebuild bcrypt" in dockerfile
    assert "npm rebuild skia-canvas" in dockerfile
    assert "node -e \"require('bcrypt')\"" in dockerfile
    assert "node -e \"require('skia-canvas')\"" in dockerfile
    assert "gateway-nginx-bootstrap.log /var/log/nginx/error.log" in dockerfile
    assert "cp -R --no-preserve=mode,ownership,timestamps /opt/floodman/gauzy-public-seed/." in dockerfile
    assert "cp -a /opt/floodman/gauzy-public-seed/\\." in dockerfile
    assert '--encoding=UTF8 --locale=C.UTF-8' in dockerfile
    assert 'floodman?client_encoding=utf8' in dockerfile
    assert "PUBLIC_BASE_URL=\"${VOICE_PUBLIC_BASE_URL" in (project_root / "unified" / "start-voice-control.sh").read_text(encoding="utf-8")
    assert "program:voice-llama" in supervisor
    assert "program:voice-control" in supervisor
    assert "program:voice-asterisk" in supervisor
    assert "program:unified-public-gateway" in supervisor
    assert "/usr/sbin/nginx -e " not in supervisor

    documenso = (project_root / "unified" / "start-documenso.sh").read_text(encoding="utf-8")
    assert "20260302223702_optimize_recipient_indexes" in documenso
    assert "prisma migrate resolve" in documenso
    assert "--rolled-back" in documenso
    assert "start-documenso-upstream.sh" in documenso

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
