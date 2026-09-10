from pathlib import Path


def test_unified_image_runs_voice_and_business_suite_on_distinct_ports(project_root: Path):
    dockerfile = (project_root / "unified" / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (project_root / "unified" / "entrypoint.sh").read_text(encoding="utf-8")
    start_hub = (project_root / "unified" / "start-hub.sh").read_text(encoding="utf-8")
    supervisor = (project_root / "unified" / "voice-supervisor.conf").read_text(encoding="utf-8")

    assert "floodman-operations:4.7.3@sha256:ebe4732d8720cf6d64c5186f3074038ae04426670910721f1636afcf7ba59472" in dockerfile
    assert "nvidia/cuda:12.4.1-runtime-ubuntu22.04@sha256:" in dockerfile
    assert "8002/tcp 9000/tcp 9001/tcp 9002/tcp 9003/tcp 9004/tcp" in dockerfile
    assert "AUDIOSOCKET_PORT=8091" in entrypoint
    assert "WEB_PORT=8802" in entrypoint
    assert "BUSINESS_SUITE_EVENTS_URL=\"http://127.0.0.1:9004/" in entrypoint
    assert "export VIRTUAL_ENV=/opt/voice-venv" in entrypoint
    assert 'export TZ="${FLOODMAN_TIMEZONE:-America/Detroit}"' in entrypoint
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
    assert '"${FM_DATA}/runtime"' in entrypoint
    assert '"${FM_RUN}/gauzy-finalized"' in entrypoint
    assert '"${FM_RUN}/owner-linked"' in entrypoint
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
    assert 'make -C contrib/uuid-ossp -j"$(nproc)"' in dockerfile
    assert "make -C contrib/uuid-ossp install" in dockerfile
    assert "--with-uuid=e2fs" in dockerfile
    assert "npm_config_build_from_source=true" in dockerfile
    assert "npm rebuild bcrypt" in dockerfile
    assert "npm rebuild skia-canvas" in dockerfile
    assert "npx --no-install prisma generate" in dockerfile
    assert "node -e \"require('bcrypt')\"" in dockerfile
    assert "node -e \"require('skia-canvas')\"" in dockerfile
    assert "gateway-nginx-bootstrap.log /var/log/nginx/error.log" in dockerfile
    assert "COPY unified/patch-office-runtime.py /opt/floodman/unified/" in dockerfile
    assert "python3 /opt/floodman/unified/patch-office-runtime.py" in dockerfile
    assert "cp -R --no-preserve=mode,ownership,timestamps" in dockerfile
    assert "/opt/floodman/gauzy-public-seed/." in dockerfile
    assert "cp -a /opt/floodman/gauzy-public-seed/\\." not in dockerfile
    image_contract = (project_root / "unified" / "image-contract.sh").read_text(encoding="utf-8")
    assert "cp -R --no-preserve=mode,ownership,timestamps" in image_contract
    assert "/opt/floodman/gauzy-public-seed/." in image_contract
    assert '--encoding=UTF8 --locale=C.UTF-8' in dockerfile
    assert 'floodman?client_encoding=utf8' in dockerfile
    assert "export FLOODMAN_COMPANY_NAME" in dockerfile
    assert "unified/start-hub.sh" in dockerfile
    assert "gauzy-web-active" in dockerfile
    assert 'chmod -R a+rX "$runtime_web"' in start_hub
    assert 'chmod 0644 "$FM_HOME/runtime/hub/floodman-hub-config.js"' in start_hub
    assert "runtime_root=/tmp/floodman-business-runtime/floodman-operations-v4.7.3" in dockerfile
    assert "orchestrator messaging-ai competitor-intel office-console local-lab" in dockerfile
    assert "sha256sum -c MANIFEST.sha256" in dockerfile
    assert "FLOODMAN_MOBILE_TOKEN_SECRET" in entrypoint
    assert "FLOODMAN_MOBILE_API_PUBLIC_URL" in entrypoint
    assert 'nginx -e "$FM_LOGS/nginx-bootstrap.log" -c' in dockerfile
    assert "! grep -Fq 'nginx -e ' /opt/floodman/aio/start-hub.sh" in dockerfile
    assert "documenso/license.json" in dockerfile
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
    assert ".floodman-recovered-$failed_migration" in documenso

    bootstrap = (project_root / "unified" / "bootstrap-databases.sh").read_text(encoding="utf-8")
    assert "GRANT USAGE, CREATE ON SCHEMA public TO floodman" in bootstrap
    assert bootstrap.index("GRANT USAGE, CREATE ON SCHEMA public TO floodman") < bootstrap.index(
        "Applying the fixed Floodman v3 database baseline"
    )

    gateway = (project_root / "unified" / "gateway-nginx.conf").read_text(encoding="utf-8")
    assert "server_name aicall.oninetwork.com" in gateway
    assert "proxy_pass http://127.0.0.1:8802" in gateway
    assert "server_name floodman.oninetwork.com" in gateway
    assert "proxy_pass http://127.0.0.1:9000" in gateway
    assert "location = /office/labelHere" in gateway
    assert "return 303 /office/desktop" in gateway
    assert "server_name sign.oninetwork.com" in gateway
    assert "server_name lab.oninetwork.com" in gateway
    assert "server_name api.oninetwork.com" in gateway
    assert "location ^~ /mobile-api/" in gateway
    assert "proxy_pass http://127.0.0.1:8700" in gateway
    assert "listen 0.0.0.0:9004" in gateway
    assert "absolute_redirect off" in gateway
    assert "proxy_pass http://127.0.0.1:8701" in gateway

    orchestrator_start = (project_root / "unified" / "start-orchestrator-api.sh").read_text(encoding="utf-8")
    assert "--host 127.0.0.1 --port 8701" in orchestrator_start


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
    assert "BUSINESS_RUNTIME_SHA256=f72e0894ddeb90665d04de878a59502b2d4ab81863f00c0409bbe46acbdd0839" in dockerfile
    assert 'sha256sum -c -' in dockerfile
    assert "PYTHON_VERSION=3.12.11" in dockerfile
    assert "PYTHON_SOURCE_SHA256=c30bb24b7f1e9a19b11b55a546434f74e739bb4c271a3e3a80ff4380d49f7adb" in dockerfile
    assert "COPY --from=python-build /opt/python312/ /opt/python312/" in dockerfile
    assert 'unzip -q "${business_runtime_zip}" -d "${business_overlay_next}"' in entrypoint
    assert 'sha256sum -c MANIFEST.sha256' in entrypoint
    assert 'prepare-roomflow.py' in entrypoint
    assert "const RELEASE = '4.7.2-unified.1';" not in entrypoint
    assert "floodman-operations-v4.7.3" in entrypoint
    assert '${DATA_DIR}/roomflow/current/index.html' in entrypoint


def test_unified_health_requires_both_product_surfaces(project_root: Path):
    health = (project_root / "unified" / "healthcheck.sh").read_text(encoding="utf-8")
    assert "127.0.0.1:8002/ready" in health
    assert "127.0.0.1:9000/health/live" in health
    assert "127.0.0.1:9004/health/ready" in health
