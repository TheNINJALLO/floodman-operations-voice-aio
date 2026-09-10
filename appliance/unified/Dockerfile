ARG BUSINESS_SUITE_IMAGE=ghcr.io/theninjallo/floodman-operations:4.7.3@sha256:ebe4732d8720cf6d64c5186f3074038ae04426670910721f1636afcf7ba59472
ARG NODE24_IMAGE=node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203
ARG NODE22_IMAGE=node:22.22.2-bookworm-slim@sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:12.4.1-devel-ubuntu22.04@sha256:da6791294b0b04d7e65d87b7451d6f2390b4d36225ab0701ee7dfec5769829f5
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:12.4.1-runtime-ubuntu22.04@sha256:517da2300c184c9999ec203c2665244bdebd3578d12fcc7065e83667932643d9
ARG LLAMA_CPP_COMMIT=6d05498314db1b57f81c271080018aa2d0b89be9
ARG PYTHON_VERSION=3.12.11
ARG PYTHON_SOURCE_SHA256=c30bb24b7f1e9a19b11b55a546434f74e739bb4c271a3e3a80ff4380d49f7adb
ARG POSTGRESQL_VERSION=14.18
ARG POSTGRESQL_SOURCE_SHA256=83ab29d6bfc3dc58b2ed3c664114fdfbeb6a0450c4b8d7fa69aee91e3ca14f8e
ARG BUSINESS_RUNTIME_URL=https://raw.githubusercontent.com/TheNINJALLO/floodman-business-suite-aio/0f9bc8553d776270267d7f2b7df6f857f6011bfe/deployment/releases/floodman-operations-runtime-v4.7.3.zip
ARG BUSINESS_RUNTIME_SHA256=f72e0894ddeb90665d04de878a59502b2d4ab81863f00c0409bbe46acbdd0839
ARG BUSINESS_LAUNCHER_URL=https://raw.githubusercontent.com/TheNINJALLO/floodman-business-suite-aio/0f9bc8553d776270267d7f2b7df6f857f6011bfe/deployment/releases/mobile-start-v4.7.3.sh
ARG BUSINESS_LAUNCHER_SHA256=511a87e9b771ef733c6937000db577a18c5313438495533db5fcb6add2855124

FROM ${BUSINESS_SUITE_IMAGE} AS business
FROM ${NODE24_IMAGE} AS node24
FROM ${NODE22_IMAGE} AS node22

FROM ${CUDA_DEVEL_IMAGE} AS python-build
ARG PYTHON_VERSION
ARG PYTHON_SOURCE_SHA256
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl libbz2-dev libexpat1-dev libffi-dev \
      libgdbm-dev liblzma-dev libncursesw5-dev libreadline-dev libsqlite3-dev \
      libssl-dev tk-dev uuid-dev xz-utils zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN curl --fail --location --retry 4 --connect-timeout 20 --max-time 300 \
      "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz" \
      -o python.tar.xz && \
    printf '%s  %s\n' "${PYTHON_SOURCE_SHA256}" python.tar.xz | sha256sum -c - && \
    tar -xJf python.tar.xz && \
    cd "Python-${PYTHON_VERSION}" && \
    ./configure --prefix=/opt/python312 --enable-shared --with-ensurepip=install \
      LDFLAGS="-Wl,-rpath,/opt/python312/lib" && \
    make -j"$(nproc)" && make install && \
    /opt/python312/bin/python3 -c 'from datetime import UTC; print(UTC)'

FROM ${CUDA_DEVEL_IMAGE} AS llama-build
ARG LLAMA_CPP_COMMIT
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates git cmake build-essential libssl-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
RUN git clone https://github.com/ggml-org/llama.cpp.git && \
    cd llama.cpp && git checkout "${LLAMA_CPP_COMMIT}" && \
    cmake -S . -B build \
      -DGGML_NATIVE=OFF \
      -DGGML_CUDA=ON \
      -DGGML_BACKEND_DL=ON \
      -DGGML_CPU_ALL_VARIANTS=ON \
      -DLLAMA_BUILD_TESTS=OFF \
      -DLLAMA_BUILD_EXAMPLES=OFF \
      -DCMAKE_CUDA_ARCHITECTURES=86 \
      -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build --target llama-server -j"$(nproc)" && \
    mkdir -p /out/bin /out/lib && \
    cp build/bin/llama-server /out/bin/ && \
    find build -name '*.so*' -exec cp -P {} /out/lib/ \;

FROM ${CUDA_DEVEL_IMAGE} AS postgres-build
ARG POSTGRESQL_VERSION
ARG POSTGRESQL_SOURCE_SHA256
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential bzip2 ca-certificates curl patch uuid-dev && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY unified/postgresql-pterodactyl-rootless.patch /tmp/postgresql-pterodactyl-rootless.patch
RUN curl --fail --location --retry 4 --connect-timeout 20 --max-time 300 \
      "https://ftp.postgresql.org/pub/source/v${POSTGRESQL_VERSION}/postgresql-${POSTGRESQL_VERSION}.tar.bz2" \
      -o postgresql.tar.bz2 && \
    printf '%s  %s\n' "${POSTGRESQL_SOURCE_SHA256}" postgresql.tar.bz2 | sha256sum -c - && \
    tar -xjf postgresql.tar.bz2 && \
    cd "postgresql-${POSTGRESQL_VERSION}" && \
    patch -p1 < /tmp/postgresql-pterodactyl-rootless.patch && \
    ./configure --prefix=/opt/floodman/postgresql14-panel \
      --without-readline --without-zlib --without-icu --with-uuid=e2fs && \
    make -j"$(nproc)" && make install && \
    make -C contrib/pgcrypto -j"$(nproc)" && \
    make -C contrib/pgcrypto install && \
    make -C contrib/pg_trgm -j"$(nproc)" && \
    make -C contrib/pg_trgm install && \
    make -C contrib/uuid-ossp -j"$(nproc)" && \
    make -C contrib/uuid-ossp install

FROM ${CUDA_RUNTIME_IMAGE}
ARG CONTAINER_UID=988
ARG CONTAINER_GID=988
ARG BUSINESS_RUNTIME_URL
ARG BUSINESS_RUNTIME_SHA256
ARG BUSINESS_LAUNCHER_URL
ARG BUSINESS_LAUNCHER_SHA256
ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/voice-venv \
    PATH=/opt/node24/bin:/opt/python312/bin:/usr/lib/postgresql/14/bin:/opt/voice-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    WEB_PORT=8002 \
    DATA_DIR=/home/container/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/opt/python312/lib:/opt/llama:/usr/local/cuda/lib64:${LD_LIBRARY_PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
      asterisk bash build-essential ca-certificates cargo coreutils curl espeak-ng ffmpeg \
      gettext-base git jq libffi-dev libgomp1 libsndfile1 libssl-dev netcat-openbsd \
      libbz2-1.0 libexpat1 libgdbm6 liblzma5 libncursesw6 libreadline8 libsqlite3-0 libuuid1 zlib1g \
      nginx openssl postgresql-14 postgresql-client-14 postgresql-contrib python3 \
      python3-dev python3-pip python3-venv supervisor tzdata unzip && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${CONTAINER_GID}" container && \
    useradd --uid "${CONTAINER_UID}" --gid "${CONTAINER_GID}" \
      --create-home --home-dir /home/container --shell /bin/bash container

COPY --from=node24 /usr/local/ /opt/node24/
COPY --from=node22 /usr/local/ /opt/node22/
COPY --from=python-build /opt/python312/ /opt/python312/
COPY --from=llama-build /out/bin/llama-server /opt/llama/llama-server
COPY --from=llama-build /out/lib/ /opt/llama/
COPY --from=postgres-build /opt/floodman/postgresql14-panel/ /opt/floodman/postgresql14-panel/

# Business Suite application source and upstream application payloads. Python
# and Node native dependencies are rebuilt for Ubuntu/glibc below; Alpine
# runtime binaries from the Business Suite image are never executed here.
COPY --from=business --chown=container:container /opt/floodman/ /opt/floodman/
COPY --from=business --chown=container:container /srv/gauzy/ /srv/gauzy/
COPY --from=business --chown=container:container /opt/gauzy-web-pristine/ /opt/gauzy-web-pristine/
COPY --from=business --chown=container:container /opt/documenso/ /opt/documenso/
COPY --from=business /usr/local/bin/mailpit /usr/local/bin/mailpit

# The pinned Suite image uses archive-copy semantics for Gauzy's public seed.
# On a Wings bind mount that can replace the destination metadata and make the
# following marker write fail. Keep the payload, never the image-layer owner.
RUN set -eux; \
    grep -Fq 'cp -a /opt/floodman/gauzy-public-seed/.' /opt/floodman/aio/start-suite.sh; \
    sed -i 's#cp -a /opt/floodman/gauzy-public-seed/\. "\$FM_DATA/gauzy-files/" 2>/dev/null || true#cp -R --no-preserve=mode,ownership,timestamps /opt/floodman/gauzy-public-seed/. "\$FM_DATA/gauzy-files/" 2>/dev/null || true#' \
      /opt/floodman/aio/start-suite.sh; \
    grep -Fq 'cp -R --no-preserve=mode,ownership,timestamps /opt/floodman/gauzy-public-seed/.' \
      /opt/floodman/aio/start-suite.sh; \
    grep -Fq 'initdb -D "$PGDATA" -U postgres --pwfile="$pwfile"' /opt/floodman/aio/start-suite.sh; \
    sed -i 's#initdb -D "$PGDATA" -U postgres #initdb -D "$PGDATA" -U postgres --encoding=UTF8 --locale=C.UTF-8 #' \
      /opt/floodman/aio/start-suite.sh; \
    sed -i 's#@127.0.0.1:5432/floodman"#@127.0.0.1:5432/floodman?client_encoding=utf8"#g' \
      /opt/floodman/aio/start-suite.sh; \
    sed -i '/^: "${FLOODMAN_COMPANY_NAME:=Floodman}"$/a export FLOODMAN_COMPANY_NAME' \
      /opt/floodman/aio/start-suite.sh; \
    grep -Fq 'floodman?client_encoding=utf8' /opt/floodman/aio/start-suite.sh; \
    grep -Fq 'export FLOODMAN_COMPANY_NAME' /opt/floodman/aio/start-suite.sh; \
    grep -Fq 'exec nginx -e "$FM_LOGS/nginx-bootstrap.log" -c' /opt/floodman/aio/start-hub.sh; \
    sed -i 's#exec nginx -e "$FM_LOGS/nginx-bootstrap.log" -c#exec nginx -c#' \
      /opt/floodman/aio/start-hub.sh; \
    ! grep -Fq 'nginx -e ' /opt/floodman/aio/start-hub.sh

# Carry the exact browser/RoomFlow payload that the standalone Business Suite
# egg normally installs into the Pterodactyl volume. The unified server has no
# separate egg install phase, so it initializes this verified payload itself.
RUN set -eux; \
    mkdir -p /opt/floodman/unified/assets; \
    curl --fail --location --retry 4 --connect-timeout 20 --max-time 300 \
      "${BUSINESS_RUNTIME_URL}" \
      -o /opt/floodman/unified/assets/floodman-operations-runtime-v4.7.3.zip; \
    printf '%s  %s\n' "${BUSINESS_RUNTIME_SHA256}" \
      /opt/floodman/unified/assets/floodman-operations-runtime-v4.7.3.zip \
      | sha256sum -c -; \
    curl --fail --location --retry 4 --connect-timeout 20 --max-time 300 \
      "${BUSINESS_LAUNCHER_URL}" -o /tmp/mobile-start-v4.7.3.sh; \
    printf '%s  %s\n' "${BUSINESS_LAUNCHER_SHA256}" /tmp/mobile-start-v4.7.3.sh \
      | sha256sum -c -; \
    awk '/^cat > .*floodman-boot-guard.js.*BOOT_GUARD/{copy=1;next} /^BOOT_GUARD$/{copy=0} copy' \
      /tmp/mobile-start-v4.7.3.sh > /opt/floodman/unified/assets/floodman-boot-guard.js; \
    awk '/^cat > .*floodman-status.html.*STATUS_PAGE/{copy=1;next} /^STATUS_PAGE$/{copy=0} copy' \
      /tmp/mobile-start-v4.7.3.sh > /opt/floodman/unified/assets/floodman-status.html; \
    test -s /opt/floodman/unified/assets/floodman-boot-guard.js; \
    test -s /opt/floodman/unified/assets/floodman-status.html; \
    mkdir -p /tmp/floodman-business-runtime; \
    unzip -q /opt/floodman/unified/assets/floodman-operations-runtime-v4.7.3.zip \
      -d /tmp/floodman-business-runtime; \
    runtime_root=/tmp/floodman-business-runtime/floodman-operations-v4.7.3; \
    test "$(cat "$runtime_root/VERSION")" = '4.7.3'; \
    (cd "$runtime_root" && sha256sum -c MANIFEST.sha256 >/dev/null); \
    for service in orchestrator messaging-ai competitor-intel office-console local-lab hub database public-signing resources; do \
      test -d "$runtime_root/$service"; \
      rm -rf "/opt/floodman/$service"; \
      mkdir -p "/opt/floodman/$service"; \
      cp -R --no-preserve=mode,ownership,timestamps "$runtime_root/$service/." "/opt/floodman/$service/"; \
    done; \
    rm -rf /tmp/floodman-business-runtime; \
    rm -f /tmp/mobile-start-v4.7.3.sh

COPY pyproject.toml /tmp/voice-source/pyproject.toml
COPY app /tmp/voice-source/app
COPY scripts /tmp/voice-source/scripts
COPY config /tmp/voice-source/config
COPY knowledge /tmp/voice-source/knowledge
COPY templates /tmp/voice-source/templates
COPY static /tmp/voice-source/static
COPY THIRD_PARTY_LICENSES.md README.md /tmp/voice-source/

RUN python3 -m venv /opt/voice-venv && \
    /opt/voice-venv/bin/pip install --upgrade pip setuptools wheel && \
    cd /tmp/voice-source && /opt/voice-venv/bin/pip install . && \
    mkdir -p /opt/voice /opt/floodman/scripts && \
    cp -a /tmp/voice-source/app /tmp/voice-source/config /tmp/voice-source/knowledge \
      /tmp/voice-source/templates /tmp/voice-source/static /tmp/voice-source/README.md \
      /tmp/voice-source/THIRD_PARTY_LICENSES.md /opt/voice/ && \
    cp -a /tmp/voice-source/scripts/. /opt/floodman/scripts/ && \
    rm -rf /tmp/voice-source

RUN set -eux; \
    for service in orchestrator messaging-ai competitor-intel office-console local-lab; do \
      rm -rf "/opt/pydeps/${service}"; \
      python3 -m pip install --no-cache-dir \
        --target "/opt/pydeps/${service}" \
        -r "/opt/floodman/requirements/${service}.txt" \
        -c "/opt/floodman/requirements/constraints/${service}.txt"; \
    done

# Rebuild native Node addons against glibc using the exact Node major each
# upstream application was published with.
RUN set -eux; \
    cd /srv/gauzy; PATH=/opt/node24/bin:$PATH npm rebuild --omit=dev --foreground-scripts; \
    find /srv/gauzy/node_modules/bcrypt -type f -name '*.node' -delete; \
    cd /srv/gauzy; PATH=/opt/node24/bin:$PATH npm_config_build_from_source=true \
      npm rebuild bcrypt --omit=dev --foreground-scripts; \
    PATH=/opt/node24/bin:$PATH node -e "require('bcrypt')"; \
    cd /opt/documenso; PATH=/opt/node22/bin:$PATH npm rebuild --omit=dev --foreground-scripts; \
    find /opt/documenso/node_modules/skia-canvas -type f -name '*.node' -delete; \
    cd /opt/documenso; PATH=/opt/node22/bin:$PATH \
      npm rebuild skia-canvas --omit=dev --foreground-scripts; \
    cd /opt/documenso/apps/remix; \
      NEXT_PRIVATE_DATABASE_URL=postgresql://documenso:build-only@127.0.0.1:5432/documenso \
      NEXT_PRIVATE_DIRECT_DATABASE_URL=postgresql://documenso:build-only@127.0.0.1:5432/documenso \
      PATH=/opt/node22/bin:$PATH npx --no-install prisma generate \
        --schema ../../packages/prisma/schema.prisma; \
    PATH=/opt/node22/bin:$PATH node -e "require('skia-canvas')"; \
    mv /opt/floodman/aio/start-documenso.sh /opt/floodman/aio/start-documenso-upstream.sh

COPY unified/entrypoint.sh /opt/floodman/scripts/entrypoint.sh
COPY unified/start-voice-control.sh unified/start-voice-llama.sh unified/start-voice-asterisk.sh \
     unified/start-documenso.sh unified/start-hub.sh unified/start-orchestrator-api.sh unified/healthcheck.sh \
     unified/image-contract.sh /opt/floodman/unified/
COPY unified/patch-office-runtime.py /opt/floodman/unified/
COPY unified/postgres-init-wrapper.sh /opt/floodman/unified/bin/initdb
COPY unified/postgres-server-wrapper.sh /opt/floodman/unified/bin/postgres
COPY unified/bootstrap-databases.sh /opt/floodman/aio/bootstrap-databases.sh
COPY unified/gateway-nginx.conf unified/proxy-headers.conf /opt/floodman/unified/
COPY unified/voice-supervisor.conf /opt/floodman/aio/voice-supervisor.conf

RUN python3 /opt/floodman/unified/patch-office-runtime.py \
      --office-root /opt/floodman/office-console

RUN set -eux; \
    cat /opt/floodman/aio/voice-supervisor.conf >> /opt/floodman/aio/supervisord.conf; \
    mkdir -p /usr/share/asterisk/sounds/en /home/container/data/business; \
    rm -f /srv/gauzy/apps/api/public /import; \
    ln -s /home/container/data/business/gauzy-files /srv/gauzy/apps/api/public; \
    ln -s /home/container/data/business/gauzy-import /import; \
    ln -s /opt/floodman/unified/start-documenso.sh /opt/floodman/aio/start-documenso.sh; \
    rm -f /opt/floodman/aio/start-hub.sh; \
    ln -s /opt/floodman/unified/start-hub.sh /opt/floodman/aio/start-hub.sh; \
    rm -f /opt/floodman/aio/start-orchestrator-api.sh; \
    ln -s /opt/floodman/unified/start-orchestrator-api.sh /opt/floodman/aio/start-orchestrator-api.sh; \
    sed -i 's#root /home/container/runtime/gauzy-web;#root /home/container/runtime/gauzy-web-active;#' \
      /opt/floodman/aio/nginx.conf.template; \
    rm -f /opt/documenso/apps/remix/.documenso-license.json; \
    ln -s /home/container/data/business/documenso/license.json \
      /opt/documenso/apps/remix/.documenso-license.json; \
    rm -f /var/log/nginx/error.log; \
    ln -s /home/container/data/business/logs/gateway-nginx-bootstrap.log /var/log/nginx/error.log; \
    espeak-ng -s 155 -v en-us -w /tmp/floodman-technical-failure-source.wav \
      "We're having a temporary technical issue. I've alerted the Floodman team. Please expect a callback as soon as possible."; \
    ffmpeg -hide_banner -loglevel error -y \
      -i /tmp/floodman-technical-failure-source.wav -ar 8000 -ac 1 -c:a pcm_s16le \
      /usr/share/asterisk/sounds/en/floodman-technical-failure.wav; \
    rm -f /tmp/floodman-technical-failure-source.wav; \
    ln -s data/business/config /home/container/config; \
    ln -s data/business/run /home/container/run; \
    ln -s data/business/logs /home/container/logs; \
    ln -s data/business/runtime /home/container/runtime; \
    ln -s data/business/backups /home/container/backups; \
    ln -s data/business/diagnostics /home/container/diagnostics; \
    ln -s data/business/tmp /home/container/tmp; \
    chmod 0755 /usr/local/bin/mailpit /opt/floodman/aio/*.sh /opt/floodman/scripts/*.sh \
      /opt/floodman/scripts/*.py /opt/floodman/unified/*.sh /opt/floodman/unified/bin/*; \
    chown -R container:container /home/container /opt/floodman /opt/voice /opt/voice-venv \
      /opt/pydeps /opt/llama /srv/gauzy /opt/gauzy-web-pristine /opt/documenso

RUN /opt/floodman/unified/image-contract.sh

LABEL org.opencontainers.image.title="Floodman Unified Voice AIO and Business Suite" \
      org.opencontainers.image.description="One Pterodactyl runtime for local GPU voice, Office, RoomFlow, ERP, signing, notifications, and API." \
      org.opencontainers.image.version="4.7.3-unified"

USER container
WORKDIR /home/container
EXPOSE 8002/tcp 9000/tcp 9001/tcp 9002/tcp 9003/tcp 9004/tcp 5060/udp 5060/tcp 10000-10100/udp
HEALTHCHECK --interval=30s --timeout=12s --start-period=900s --retries=5 CMD /opt/floodman/unified/healthcheck.sh
ENTRYPOINT ["/opt/floodman/scripts/entrypoint.sh"]
