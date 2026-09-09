#!/bin/sh
set -eu
. /opt/floodman/aio/common.sh

while [ ! -f "$FM_RUN/gauzy-finalized" ]; do sleep 2; done
fm_wait_http "http://127.0.0.1:8700/health/live" 600 false \
  || fm_die "Floodman Office did not become ready before Hub startup."

# Older images created runtime/gauzy-web as a different numeric owner. Wings
# removes CAP_DAC_OVERRIDE/CAP_FOWNER, so even the root-launched unified image
# cannot safely delete that tree. Build each immutable browser configuration in
# a content-addressed directory and atomically switch a symlink in its parent.
runtime_root="$FM_HOME/runtime/gauzy-web-unified"
config_hash="$(printf '%s\n' "$MAIN_PUBLIC_URL|$FLOODMAN_COMPANY_NAME|$HUB_RELEASE" | sha256sum | awk '{print $1}')"
runtime_web="$runtime_root/$config_hash"
active_link="$FM_HOME/runtime/gauzy-web-active"
mkdir -p "$runtime_root"

if [ ! -s "$runtime_web/index.html" ] || [ ! -s "$runtime_web/.floodman-config-hash" ]; then
  fm_log "Preparing the branded Floodman ERP browser bundle..."
  if [ -e "$runtime_web" ] || [ -L "$runtime_web" ]; then
    mv "$runtime_web" "$runtime_root/.incomplete-$config_hash-$(date +%s)-$$"
  fi
  stage="$(mktemp -d "$runtime_root/.stage-XXXXXX")"
  cp -R --no-preserve=mode,ownership,timestamps /opt/gauzy-web-pristine/. "$stage/"
  cd "$stage"
  envsubst < replacements.sed > replacements_values.sed
  for file in ./*.js; do
    [ -f "$file" ] || continue
    sed -i -f replacements_values.sed "$file"
  done
  printf '%s' "$config_hash" > .floodman-config-hash
  mv "$stage" "$runtime_web"
fi

active_next="$FM_HOME/runtime/.gauzy-web-active.$$"
ln -s "$runtime_web" "$active_next"
mv -Tf "$active_next" "$active_link"

mkdir -p "$FM_HOME/runtime/hub"
envsubst '${HUB_TITLE} ${HUB_RELEASE} ${HUB_OFFICE_URL} ${HUB_VOICE_URL} ${HUB_ROOMFLOW_URL} ${HUB_DOCUMENSO_URL} ${HUB_MAILPIT_URL} ${HUB_ENGINEERING_URL} ${HUB_API_URL} ${HUB_COMPETITOR_URL} ${HUB_SYNC_STATUS_URL} ${HUB_REMOTE_ACCESS_ENABLED} ${HUB_REMOTE_DOCUMENSO_PORT} ${HUB_REMOTE_MAILPIT_PORT} ${HUB_REMOTE_ENGINEERING_PORT} ${HUB_REMOTE_API_PORT} ${HUB_REMOTE_DOCUMENSO_URL} ${HUB_REMOTE_MAILPIT_URL} ${HUB_REMOTE_ENGINEERING_URL} ${HUB_REMOTE_API_URL}' \
  < /opt/floodman/hub/hub-config.js.template \
  > "$FM_HOME/runtime/hub/floodman-hub-config.js"
envsubst '${SERVER_PORT} ${HUB_RELEASE}' \
  < /opt/floodman/aio/nginx.conf.template \
  > "$FM_CONFIG/nginx.conf"

fm_log "Starting Floodman Operations Hub on port $SERVER_PORT..."
exec nginx -c "$FM_CONFIG/nginx.conf" -g 'daemon off;'
