from __future__ import annotations

from pathlib import Path


def test_full_image_uses_python_312_compatible_piper(project_root: Path) -> None:
    dockerfile = (project_root / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG PIPER_TTS_VERSION=1.6.0" in dockerfile
    assert "piper-tts==${PIPER_TTS_VERSION}" in dockerfile
    assert "piper-tts==1.2.0" not in dockerfile
    assert "PiperVoice, 'synthesize_wav'" in dockerfile


def test_ava_patch_covers_current_piper_api(project_root: Path) -> None:
    patch = (project_root / "scripts/patch_ava.py").read_text(encoding="utf-8")
    assert "Floodman Piper API compatibility patch" in patch
    assert 'synthesize_wav = getattr(self.tts_model' in patch
    assert "local_ai_server" in patch
    assert "server.py" in patch


def test_lite_and_full_images_publish_independently(project_root: Path) -> None:
    workflow = (project_root / ".github/workflows/ci-container.yml").read_text(
        encoding="utf-8"
    )
    assert "publish-lite:" in workflow
    assert "publish-full:" in workflow
    assert "docker/build-push-action@v6" in workflow
    assert "target: lite" in workflow
    assert "target: full" in workflow
