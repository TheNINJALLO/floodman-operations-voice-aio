FROM ubuntu:24.04 AS base

ARG DEBIAN_FRONTEND=noninteractive
ARG AVA_COMMIT=18d4e769335f3b643aca80e084c7e66f0969491e
ARG PRELOAD_WHISPER_MODEL=tiny.en
ARG PIPER_TTS_VERSION=1.6.0
ARG LLAMA_CPP_PYTHON_VERSION=0.3.34

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    FLOODMAN_PROJECT_ROOT=/opt/floodman \
    DATA_DIR=/home/container/data \
    WEB_PORT=9000 \
    HF_HOME=/opt/model-cache/huggingface \
    XDG_CACHE_HOME=/opt/model-cache/cache

RUN apt-get update && apt-get install -y --no-install-recommends \
    asterisk \
    asterisk-core-sounds-en-wav \
    build-essential \
    ca-certificates \
    cmake \
    curl \
    ffmpeg \
    git \
    libasound2-dev \
    libffi-dev \
    libgomp1 \
    libportaudio2 \
    libsndfile1 \
    ninja-build \
    pkg-config \
    python3 \
    python3-dev \
    python3-pip \
    python3-venv \
    sox \
    supervisor \
    unzip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv && pip install --upgrade pip setuptools wheel

COPY scripts/patch_ava.py /opt/floodman-build/patch_ava.py
COPY scripts/patch_ava_production.py /opt/floodman-build/patch_ava_production.py

RUN git init /opt/ava \
    && git -C /opt/ava remote add origin https://github.com/hkjarral/AVA-AI-Voice-Agent-for-Asterisk.git \
    && git -C /opt/ava fetch --depth 1 origin ${AVA_COMMIT} \
    && git -C /opt/ava checkout --detach FETCH_HEAD \
    && test "$(git -C /opt/ava rev-parse HEAD)" = "${AVA_COMMIT}" \
    && test -f /opt/ava/local_ai_server/server.py \
    && python3 /opt/floodman-build/patch_ava.py --ava-root /opt/ava \
    && python3 /opt/floodman-build/patch_ava_production.py --ava-root /opt/ava \
    && grep -q "Floodman JSON body safety patch" /opt/ava/src/tools/http/in_call_lookup.py \
    && grep -q "Floodman Piper API compatibility patch" /opt/ava/local_ai_server/server.py \
    && grep -q "Floodman Groq reasoning controls patch" /opt/ava/src/pipelines/openai.py \
    && rm -f /opt/floodman-build/patch_ava.py \
       /opt/floodman-build/patch_ava_production.py

RUN pip install --no-cache-dir -r /opt/ava/requirements.txt \
    && pip install --no-cache-dir -r /opt/ava/local_ai_server/requirements-base.txt \
    && pip install --no-cache-dir "faster-whisper>=1.1,<2"

WORKDIR /opt/floodman
COPY . /opt/floodman
RUN pip install --no-cache-dir /opt/floodman \
    && chmod +x /opt/floodman/scripts/*.sh /opt/floodman/scripts/agi_*.py /opt/floodman/scripts/render_asterisk.py

FROM base AS runtime-base

RUN useradd --create-home --home-dir /home/container --uid 988 --shell /bin/bash container \
    && mkdir -p /opt/model-cache /home/container/data/runtime/supervisor \
    && chown -R container:container /home/container /opt/ava /opt/floodman /opt/model-cache

EXPOSE 9000/tcp 5060/udp 5061/tcp 10000-10040/udp
# Do not declare /home/container or a child as a Docker VOLUME.
# Pterodactyl bind-mounts /home/container and its File Manager reads that mount.
HEALTHCHECK --interval=30s --timeout=8s --start-period=120s --retries=4 \
  CMD curl -fsS http://127.0.0.1:${WEB_PORT}/livez || exit 1

FROM runtime-base AS lite

ENV IMAGE_FLAVOR=lite \
    AUTO_INSTALL_LOCAL_MODELS=false

RUN if [ -n "$PRELOAD_WHISPER_MODEL" ]; then \
      /opt/venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('${PRELOAD_WHISPER_MODEL}', device='cpu', compute_type='int8')"; \
    fi \
    && chown -R container:container /opt/model-cache

USER container
ENTRYPOINT ["/opt/floodman/scripts/entrypoint.sh"]

FROM runtime-base AS full

RUN pip install --no-cache-dir vosk==0.3.45 "piper-tts==${PIPER_TTS_VERSION}" \
    && python -c "from piper import PiperVoice; assert callable(getattr(PiperVoice, 'load', None)); assert callable(getattr(PiperVoice, 'synthesize_wav', None))" \
    && pip install --no-cache-dir "diskcache>=5.6.1,<6" \
    && CFLAGS="-O3 -march=x86-64 -mtune=generic" \
       CXXFLAGS="-O3 -march=x86-64 -mtune=generic" \
       CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_SSE42=OFF -DGGML_AVX=OFF -DGGML_AVX_VNNI=OFF -DGGML_AVX2=OFF -DGGML_BMI2=OFF -DGGML_FMA=OFF -DGGML_F16C=OFF -DGGML_AVX512=OFF -DGGML_AVX512_VBMI=OFF -DGGML_AVX512_VNNI=OFF -DGGML_AVX512_BF16=OFF -DGGML_AMX_TILE=OFF -DGGML_AMX_INT8=OFF -DGGML_AMX_BF16=OFF -DGGML_LTO=OFF -DGGML_BLAS=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF" \
       pip install --no-cache-dir --force-reinstall --no-deps --no-binary=llama-cpp-python "llama-cpp-python==${LLAMA_CPP_PYTHON_VERSION}" \
    && python -c "import diskcache, jinja2, numpy, typing_extensions; from llama_cpp import Llama; assert Llama is not None" \
    && if [ -n "$PRELOAD_WHISPER_MODEL" ]; then \
         /opt/venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('${PRELOAD_WHISPER_MODEL}', device='cpu', compute_type='int8')"; \
       fi \
    && chown -R container:container /opt/model-cache

ENV IMAGE_FLAVOR=full

USER container
ENTRYPOINT ["/opt/floodman/scripts/entrypoint.sh"]
