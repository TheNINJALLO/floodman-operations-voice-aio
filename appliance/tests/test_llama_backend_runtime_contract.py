from __future__ import annotations

from pathlib import Path


def test_dynamic_backends_are_next_to_llama_server(
    project_root: Path,
) -> None:
    dockerfile = (
        project_root / "Dockerfile"
    ).read_text(encoding="utf-8")

    assert "-DGGML_CUDA=ON" in dockerfile
    assert "-DGGML_BACKEND_DL=ON" in dockerfile
    assert (
        "COPY --from=llama-build /out/lib/ /opt/llama/"
        in dockerfile
    )
    assert (
        "COPY --from=llama-build /out/lib/ /opt/llama/lib/"
        not in dockerfile
    )
    assert (
        "LD_LIBRARY_PATH=/opt/llama:/usr/local/cuda/lib64:"
        in dockerfile
    )


def test_llama_launcher_uses_current_cli_and_backend_directory(
    project_root: Path,
) -> None:
    launcher = (
        project_root / "scripts/llama-server.sh"
    ).read_text(encoding="utf-8")

    assert 'LLAMA_RUNTIME_DIR="${LLAMA_RUNTIME_DIR:-/opt/llama}"' in launcher
    assert 'cd "${LLAMA_RUNTIME_DIR}"' in launcher
    assert 'exec "${LLAMA_RUNTIME_DIR}/llama-server"' in launcher
    assert "--alias " in launcher
    assert "--model-alias" not in launcher
