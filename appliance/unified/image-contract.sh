#!/bin/bash
set -euo pipefail
test -x /opt/voice-venv/bin/python
command -v setpriv >/dev/null
test -x /opt/floodman/unified/bin/initdb
test -x /opt/floodman/unified/bin/postgres
test -x /opt/floodman/postgresql14-panel/bin/initdb
test -x /opt/floodman/postgresql14-panel/bin/postgres
test -s /opt/floodman/postgresql14-panel/share/extension/pgcrypto.control
test -s /opt/floodman/postgresql14-panel/lib/pgcrypto.so
test -s /opt/floodman/postgresql14-panel/share/extension/pg_trgm.control
test -s /opt/floodman/postgresql14-panel/lib/pg_trgm.so
test -s /opt/floodman/postgresql14-panel/share/extension/uuid-ossp.control
test -s /opt/floodman/postgresql14-panel/lib/uuid-ossp.so
grep -Fq '/opt/floodman/unified/bin:/opt/node24/bin' /opt/floodman/scripts/entrypoint.sh
grep -Fq 'share_panel_directory()' /opt/floodman/scripts/entrypoint.sh
grep -Fq 'setpriv --reuid="${owner}" --regid="${group}"' /opt/floodman/scripts/entrypoint.sh
grep -Fq '"${FM_DATA}/gauzy-files"' /opt/floodman/scripts/entrypoint.sh
grep -Fq '"${FM_RUN}/gauzy-finalized"' /opt/floodman/scripts/entrypoint.sh
grep -Fq '"${FM_RUN}/owner-linked"' /opt/floodman/scripts/entrypoint.sh
! grep -Fq 'share_panel_directory "${FM_CONFIG}"' /opt/floodman/scripts/entrypoint.sh
grep -Fq 'cp -R --no-preserve=mode,ownership,timestamps /opt/floodman/gauzy-public-seed/.' /opt/floodman/aio/start-suite.sh
! grep -Fq 'cp -a /opt/floodman/gauzy-public-seed/.' /opt/floodman/aio/start-suite.sh
grep -Fq -- '--encoding=UTF8 --locale=C.UTF-8' /opt/floodman/aio/start-suite.sh
test "$(grep -Fc 'floodman?client_encoding=utf8' /opt/floodman/aio/start-suite.sh)" -eq 2
grep -Fq 'export FLOODMAN_COMPANY_NAME' /opt/floodman/aio/start-suite.sh
test "$(readlink /opt/floodman/aio/start-hub.sh)" = "/opt/floodman/unified/start-hub.sh"
grep -Fq 'runtime/gauzy-web-active' /opt/floodman/aio/start-hub.sh
grep -Fq 'root /home/container/runtime/gauzy-web-active;' /opt/floodman/aio/nginx.conf.template
! grep -Fq 'nginx -e ' /opt/floodman/aio/start-hub.sh
grep -Fq 'NoNewPrivs:[[:space:]]*1' /opt/floodman/unified/bin/initdb
grep -Fq 'FLOODMAN_PTERODACTYL_ROOTLESS=1' /opt/floodman/unified/bin/postgres
grep -Fq 'export VIRTUAL_ENV=/opt/voice-venv' /opt/floodman/scripts/entrypoint.sh
grep -Fq 'readonly PYTHON_BIN=/opt/voice-venv/bin/python' /opt/floodman/scripts/entrypoint.sh
grep -Fq '/opt/python312/bin' /opt/floodman/scripts/entrypoint.sh
grep -Fq 'export APP_LOGO="${APP_LOGO:-${FLOODMAN_PUBLIC_URL}/floodman-brand/floodman-wordmark.svg}"' /opt/floodman/scripts/entrypoint.sh
grep -Fq 'cp -R --no-preserve=mode,ownership,timestamps /opt/floodman/hub/.' /opt/floodman/scripts/entrypoint.sh
! grep -Fq 'cp -a /opt/floodman/hub/.' /opt/floodman/scripts/entrypoint.sh
test -x /opt/llama/llama-server
test -x /usr/local/bin/mailpit
test -s /srv/gauzy/main.js
test -s /opt/documenso/apps/remix/start.sh
test -x /opt/floodman/unified/start-documenso.sh
test -x /opt/floodman/aio/start-documenso-upstream.sh
test "$(readlink /opt/floodman/aio/start-documenso.sh)" = "/opt/floodman/unified/start-documenso.sh"
grep -Fq '20260302223702_optimize_recipient_indexes' /opt/floodman/unified/start-documenso.sh
grep -Fq '.floodman-recovered-$failed_migration' /opt/floodman/unified/start-documenso.sh
grep -Fq 'GRANT USAGE, CREATE ON SCHEMA public TO floodman' /opt/floodman/aio/bootstrap-databases.sh
test "$(readlink /var/log/nginx/error.log)" = "/home/container/data/business/logs/gateway-nginx-bootstrap.log"
test "$(readlink /opt/documenso/apps/remix/.documenso-license.json)" = "/home/container/data/business/documenso/license.json"
test -s /opt/floodman/aio/supervisord.conf
! grep -Fq '/usr/sbin/nginx -e ' /opt/floodman/aio/supervisord.conf
test -s /opt/floodman/unified/gateway-nginx.conf
test -s /opt/floodman/unified/assets/floodman-operations-runtime-v4.7.2.zip
test -s /opt/floodman/unified/assets/floodman-boot-guard.js
test -s /opt/floodman/unified/assets/floodman-status.html
unzip -tq /opt/floodman/unified/assets/floodman-operations-runtime-v4.7.2.zip >/dev/null
grep -q 'server_name floodman.oninetwork.com' /opt/floodman/unified/gateway-nginx.conf
python3 -c 'from datetime import UTC; assert str(UTC) == "UTC"'
test "$(readlink /srv/gauzy/apps/api/public)" = "/home/container/data/business/gauzy-files"
test "$(readlink /import)" = "/home/container/data/business/gauzy-import"
/opt/voice-venv/bin/python -c 'import app, httpx, yaml' 
PYTHONPATH=/opt/pydeps/orchestrator:/opt/floodman/orchestrator python3 -c 'import app.ai_calling'
/opt/node24/bin/node --check /srv/gauzy/main.js
/opt/node24/bin/node -e "require('/srv/gauzy/node_modules/bcrypt')"
/opt/node22/bin/node -e "require('/opt/documenso/node_modules/skia-canvas')"
find /opt/documenso/node_modules/.prisma/client -maxdepth 1 -type f \
  -name '*debian-openssl-3.0.x*' -print -quit | grep -q .
/opt/node24/bin/node --version
/opt/node22/bin/node --version
postgres --version
/usr/local/bin/mailpit version
bash -n /opt/floodman/scripts/entrypoint.sh /opt/floodman/unified/*.sh
for script in /opt/floodman/aio/*.sh; do sh -n "${script}"; done
echo 'FLOODMAN_UNIFIED_IMAGE_CONTRACT_PASS'
