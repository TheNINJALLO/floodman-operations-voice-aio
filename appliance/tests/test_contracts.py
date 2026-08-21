from pathlib import Path
import json

def test_no_paid_ai_keys(project_root: Path):
    source="\n".join(path.read_text(encoding="utf-8",errors="ignore") for path in (project_root/"app").glob("*.py"))
    for name in ("GROQ_API_KEY","DEEPGRAM_API_KEY","ELEVENLABS_API_KEY","OPENAI_API_KEY"):
        assert name not in source

def test_one_unit_supervisor(project_root: Path):
    text=(project_root/"supervisor/supervisord.conf").read_text()
    assert "[program:llama]" in text
    assert "[program:floodman-control]" in text
    assert "[program:asterisk]" in text
    assert "program:ava" not in text

def test_a1000_cuda_contract(project_root: Path):
    text=(project_root/"Dockerfile").read_text()
    assert "DGGML_CUDA=ON" in text
    assert "CMAKE_CUDA_ARCHITECTURES=86" in text
    assert "ARG CONTAINER_UID=988" in text
    assert "ARG CONTAINER_GID=988" in text
    assert 'useradd --uid "${CONTAINER_UID}"' in text
    assert "6d05498314db1b57f81c271080018aa2d0b89be9" in text
    assert "Qwen3-4B-Q4_K_M.gguf" in (project_root/".env.example").read_text()

def test_egg(project_root: Path):
    egg=json.loads((project_root/"pterodactyl/egg-floodman-voice-appliance.json").read_text())
    names=[item["env_variable"] for item in egg["variables"]]
    assert len(names)==len(set(names))
    assert list(egg["docker_images"].values()) == [
        "ghcr.io/theninjallo/"
        "floodman-operations-voice-aio:gpu-appliance"
    ]
    assert "SIP_PASSWORD" in names and "KOKORO_VOICE" in names


def test_ci_workflow_template(project_root: Path):
    path = project_root / "ci/ci-gpu-appliance.yml"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "TEMPLATE ONLY" in text
    assert "Build Floodman GPU Appliance" in text
    assert "docker/build-push-action@v6" in text
