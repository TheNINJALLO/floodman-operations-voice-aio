#!/bin/bash
set -euo pipefail
test -x /opt/voice-venv/bin/python
test -x /opt/llama/llama-server
test -x /usr/local/bin/mailpit
test -s /srv/gauzy/main.js
test -s /opt/documenso/apps/remix/start.sh
test -s /opt/floodman/aio/supervisord.conf
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
