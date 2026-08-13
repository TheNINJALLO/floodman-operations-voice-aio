from __future__ import annotations

from pathlib import Path


def test_full_image_installs_llama_runtime_dependencies(
    project_root: Path,
) -> None:
    dockerfile = (project_root / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert 'pip install --no-cache-dir "diskcache>=5.6.1,<6"' in dockerfile
    assert (
        "import diskcache, jinja2, numpy, typing_extensions; "
        "from llama_cpp import Llama"
    ) in dockerfile
    assert "--no-deps --no-binary=llama-cpp-python" in dockerfile
    assert "-DGGML_NATIVE=OFF" in dockerfile
