ARG BUSINESS_SUITE_IMAGE=ghcr.io/theninjallo/floodman-operations:4.7.2@sha256:17e91a3743f7affd6bd2a211a91b082c0db917c27c1258d7298cebb6ec28638c
ARG NODE24_IMAGE=node:24.16.0-bookworm-slim@sha256:2c87ef9bd3c6a3bd4b472b4bec2ce9d16354b0c574f736c476489d09f560a203
ARG NODE22_IMAGE=node:22.22.2-bookworm-slim@sha256:9f6d5975c7dca860947d3915877f85607946403fc55349f39b4bc3688448bb6e
ARG CUDA_DEVEL_IMAGE=nvidia/cuda:12.4.1-devel-ubuntu22.04@sha256:da6791294b0b04d7e65d87b7451d6f2390b4d36225ab0701ee7dfec5769829f5
ARG CUDA_RUNTIME_IMAGE=nvidia/cuda:12.4.1-runtime-ubuntu22.04@sha256:517da2300c184c9999ec203c2665244bdebd3578d12fcc7065e83667932643d9
ARG LLAMA_CPP_COMMIT=6d05498314db1b57f81c271080018aa2d0b89be9
ARG BUSINESS_RUNTIME_URL=https://raw.githubusercontent.com/TheNINJALLO/floodman-business-suite-aio/b81f9db3bbfa221dd2e439ad28624b3066fe8b0e/deployment/releases/floodman-operations-runtime-v4.7.2.zip
ARG BUSINESS_RUNTIME_SHA256=f8d3d07ff885ecde57703abba1567d151ecc4419c5afb4a73bcb5f91a054f177
ARG BUSINESS_LAUNCHER_URL=https://raw.githubusercontent.com/TheNINJALLO/floodman-business-suite-aio/b81f9db3bbfa221dd2e439ad28624b3066fe8b0e/deployment/releases/mobile-start-v4.7.2.sh
ARG BUSINESS_LAUNCHER_SHA256=f8169ed1d039ec94db8d8fecaf8708e3d322d1dea489dff09afd2766f81c3c96

FROM ${BUSINESS_SUITE_IMAGE} AS business
FROM ${NODE24_IMAGE} AS node24
FROM ${NODE22_IMAGE} AS node22

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

FROM ${CUDA_RUNTIME_IMAGE}
ARG CONTAINER_UID=988
ARG CONTAINER_GID=988
ARG BUSINESS_RUNTIME_URL
ARG BUSINESS_RUNTIME_SHA256
ARG BUSINESS_LAUNCHER_URL
ARG BUSINESS_LAUNCHER_SHA256
ENV DEBIAN_FRONTEND=noninteractive \
    VIRTUAL_ENV=/opt/voice-venv \
    PATH=/opt/node24/bin:/usr/lib/postgresql/14/bin:/opt/voice-venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    WEB_PORT=8002 \
    DATA_DIR=/home/container/data \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/opt/llama:/usr/local/cuda/lib64:${LD_LIBRARY_PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
      asterisk bash build-essential ca-certificates cargo coreutils curl espeak-ng ffmpeg \
      gettext-base git jq libffi-dev libgomp1 libsndfile1 libssl-dev netcat-openbsd \
      nginx openssl postgresql-14 postgresql-client-14 postgresql-contrib python3 \
      python3-dev python3-pip python3-venv supervisor tzdata unzip && \
    rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "${CONTAINER_GID}" container && \
    useradd --uid "${CONTAINER_UID}" --gid "${CONTAINER_GID}" \
      --create-home --home-dir /home/container --shell /bin/bash container

COPY --from=node24 /usr/local/ /opt/node24/
COPY --from=node22 /usr/local/ /opt/node22/
COPY --from=llama-build /out/bin/llama-server /opt/llama/llama-server
COPY --from=llama-build /out/lib/ /opt/llama/

# Business Suite application source and upstream application payloads. Python
# and Node native dependencies are rebuilt for Ubuntu/glibc below; Alpine
# runtime binaries from the Business Suite image are never executed here.
COPY --from=business --chown=container:container /opt/floodman/ /opt/floodman/
COPY --from=business --chown=container:container /srv/gauzy/ /srv/gauzy/
COPY --from=business --chown=container:container /opt/gauzy-web-pristine/ /opt/gauzy-web-pristine/
COPY --from=business --chown=container:container /opt/documenso/ /opt/documenso/
COPY --from=business /usr/local/bin/mailpit /usr/local/bin/mailpit

# Carry the exact browser/RoomFlow payload that the standalone Business Suite
# egg normally installs into the Pterodactyl volume. The unified server has no
# separate egg install phase, so it initializes this verified payload itself.
RUN set -eux; \
    mkdir -p /opt/floodman/unified/assets; \
    curl --fail --location --retry 4 --connect-timeout 20 --max-time 300 \
      "${BUSINESS_RUNTIME_URL}" \
      -o /opt/floodman/unified/assets/floodman-operations-runtime-v4.7.2.zip; \
    printf '%s  %s\n' "${BUSINESS_RUNTIME_SHA256}" \
      /opt/floodman/unified/assets/floodman-operations-runtime-v4.7.2.zip \
      | sha256sum -c -; \
    curl --fail --location --retry 4 --connect-timeout 20 --max-time 300 \
      "${BUSINESS_LAUNCHER_URL}" -o /tmp/mobile-start-v4.7.2.sh; \
    printf '%s  %s\n' "${BUSINESS_LAUNCHER_SHA256}" /tmp/mobile-start-v4.7.2.sh \
      | sha256sum -c -; \
    awk '/^cat > .*floodman-boot-guard.js.*BOOT_GUARD/{copy=1;next} /^BOOT_GUARD$/{copy=0} copy' \
      /tmp/mobile-start-v4.7.2.sh > /opt/floodman/unified/assets/floodman-boot-guard.js; \
    awk '/^cat > .*floodman-status.html.*STATUS_PAGE/{copy=1;next} /^STATUS_PAGE$/{copy=0} copy' \
      /tmp/mobile-start-v4.7.2.sh > /opt/floodman/unified/assets/floodman-status.html; \
    test -s /opt/floodman/unified/assets/floodman-boot-guard.js; \
    test -s /opt/floodman/unified/assets/floodman-status.html; \
    rm -f /tmp/mobile-start-v4.7.2.sh

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

# The CUDA Ubuntu 22.04 base supplies Python 3.10. Business Suite locks were
# generated on Python 3.12 and select websockets 17, whose Linux wheels require
# a newer interpreter. Websockets 16 supports CPython 3.10 and remains within
# Uvicorn's declared >=10.4 range.
RUN set -eux; \
    sed -i 's/^websockets==17\.0\.1$/websockets==16.0/' \
      /opt/floodman/requirements/constraints/*.txt; \
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
    cd /opt/documenso; PATH=/opt/node22/bin:$PATH npm rebuild --omit=dev --foreground-scripts; \
    sed -i 's#/opt/documenso-runtime/usr/local/bin#/opt/node22/bin#' /opt/floodman/aio/start-documenso.sh

COPY unified/entrypoint.sh /opt/floodman/scripts/entrypoint.sh
COPY unified/start-voice-control.sh unified/start-voice-llama.sh unified/start-voice-asterisk.sh \
     unified/healthcheck.sh unified/image-contract.sh /opt/floodman/unified/
COPY unified/gateway-nginx.conf unified/proxy-headers.conf /opt/floodman/unified/
COPY unified/voice-supervisor.conf /opt/floodman/aio/voice-supervisor.conf

RUN set -eux; \
    cat /opt/floodman/aio/voice-supervisor.conf >> /opt/floodman/aio/supervisord.conf; \
    mkdir -p /usr/share/asterisk/sounds/en /home/container/data/business; \
    rm -f /srv/gauzy/apps/api/public /import; \
    ln -s /home/container/data/business/gauzy-files /srv/gauzy/apps/api/public; \
    ln -s /home/container/data/business/gauzy-import /import; \
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
      /opt/floodman/scripts/*.py /opt/floodman/unified/*.sh; \
    chown -R container:container /home/container /opt/floodman /opt/voice /opt/voice-venv \
      /opt/pydeps /opt/llama /srv/gauzy /opt/gauzy-web-pristine /opt/documenso

RUN /opt/floodman/unified/image-contract.sh

LABEL org.opencontainers.image.title="Floodman Unified Voice AIO and Business Suite" \
      org.opencontainers.image.description="One Pterodactyl runtime for local GPU voice, Office, RoomFlow, ERP, signing, notifications, and API." \
      org.opencontainers.image.version="4.7.2-unified"

USER container
WORKDIR /home/container
EXPOSE 8002/tcp 9000/tcp 9001/tcp 9002/tcp 9003/tcp 9004/tcp 5060/udp 5060/tcp 10000-10100/udp
HEALTHCHECK --interval=30s --timeout=12s --start-period=900s --retries=5 CMD /opt/floodman/unified/healthcheck.sh
ENTRYPOINT ["/opt/floodman/scripts/entrypoint.sh"]
