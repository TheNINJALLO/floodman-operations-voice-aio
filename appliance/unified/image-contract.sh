#!/bin/bash
set -euo pipefail
test -x /opt/voice-venv/bin/python
command -v runuser >/dev/null
grep -Fq 'runuser --user container --preserve-environment' /opt/floodman/scripts/entrypoint.sh
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
test -s /opt/floodman/aio/supervisord.conf
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
/opt/node24/bin/node --version
/opt/node22/bin/node --version
postgres --version
/usr/local/bin/mailpit version
bash -n /opt/floodman/scripts/entrypoint.sh /opt/floodman/unified/*.sh
for script in /opt/floodman/aio/*.sh; do sh -n "${script}"; done
echo 'FLOODMAN_UNIFIED_IMAGE_CONTRACT_PASS'
