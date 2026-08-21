ARG CUDA_VERSION=12.4.1
ARG UBUNTU_VERSION=22.04
ARG LLAMA_CPP_COMMIT=6d05498314db1b57f81c271080018aa2d0b89be9

FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS llama-build
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

FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}
ARG CONTAINER_UID=988
ARG CONTAINER_GID=988
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    LD_LIBRARY_PATH=/opt/llama/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH}
RUN apt-get update && apt-get install -y --no-install-recommends \
    asterisk supervisor python3 python3-pip python3-venv \
    curl ca-certificates ffmpeg espeak-ng libsndfile1 libgomp1 git && \
    rm -rf /var/lib/apt/lists/*
RUN groupadd --gid "${CONTAINER_GID}" container && \
    useradd --uid "${CONTAINER_UID}" --gid "${CONTAINER_GID}" \
      --create-home --home-dir /home/container --shell /bin/bash container
COPY --from=llama-build /out/bin/llama-server /opt/llama/llama-server
COPY --from=llama-build /out/lib/ /opt/llama/lib/
WORKDIR /opt/floodman
COPY pyproject.toml ./
COPY app ./app
COPY scripts ./scripts
COPY config ./config
COPY knowledge ./knowledge
COPY templates ./templates
COPY static ./static
COPY supervisor ./supervisor
COPY THIRD_PARTY_LICENSES.md README.md ./
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --upgrade pip setuptools wheel && \
    /opt/venv/bin/pip install . && \
    mkdir -p /home/container/data && \
    chown -R container:container /home/container /opt/floodman /opt/llama && \
    chmod +x /opt/floodman/scripts/*.sh /opt/floodman/scripts/*.py
USER container
WORKDIR /home/container
EXPOSE 8003/tcp 5060/udp 5060/tcp 10000-10100/udp
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 CMD curl -fsS http://127.0.0.1:8003/ready || exit 1
ENTRYPOINT ["/opt/floodman/scripts/entrypoint.sh"]
