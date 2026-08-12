from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _write_http_fixtures(ava_root: Path) -> None:
    http_dir = ava_root / "src/tools/http"
    http_dir.mkdir(parents=True)
    (http_dir / "in_call_lookup.py").write_text(
        '''import json\nimport os\nimport re\nfrom typing import Dict\n\nclass InCallHTTPTool:\n    def build(self, sub_context):\n        body_str = self._substitute_variables(self.config.body_template, sub_context)\n        return body_str\n\n    def _substitute_variables(self, template: str, context: Dict[str, str]) -> str:\n        return template\n''',
        encoding="utf-8",
    )
    (http_dir / "generic_lookup.py").write_text(
        '''import json\nimport os\nimport re\n\nclass PreCallContext:\n    pass\n\nclass GenericHTTPLookupTool:\n    def build(self, context):\n        body = self._substitute_variables(self.config.body_template, context)\n        return body\n\n    def _substitute_variables(self, template: str, context: PreCallContext) -> str:\n        return template\n''',
        encoding="utf-8",
    )


def _write_server_fixture(path: Path) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(
        '''from __future__ import annotations\n\nimport io\nimport wave\n\nclass LocalAIServer:\n    def __init__(self, tts_model):\n        self.tts_model = tts_model\n\n    def render(self, text):\n        wav_buf = io.BytesIO()\n        with wave.open(wav_buf, "wb") as wav_file:\n            wav_file.setnchannels(1)\n            wav_file.setsampwidth(2)\n            wav_file.setframerate(22050)\n            try:\n                self.tts_model.synthesize(text, wav_file)\n            except TypeError:\n                audio_generator = self.tts_model.synthesize(text)\n                for chunk in audio_generator:\n                    if isinstance(chunk, (bytes, bytearray)):\n                        wav_file.writeframes(chunk)\n                    else:\n                        data = getattr(chunk, "audio_int16_bytes", None)\n                        if data:\n                            wav_file.writeframes(data)\n        return wav_buf.getvalue()\n\n    def load(self):\n        raise ImportError(\n            "Piper TTS backend requested but piper-tts is not installed. "\n            "Build with INCLUDE_PIPER=true or install piper-tts==1.2.0."\n        )\n''',
        encoding="utf-8",
    )


def test_patch_prefers_modern_piper_wav_api(project_root: Path, tmp_path: Path) -> None:
    ava_root = tmp_path / "ava"
    _write_http_fixtures(ava_root)
    server_path = ava_root / "local_ai_server/server.py"
    _write_server_fixture(server_path)

    command = [
        sys.executable,
        str(project_root / "scripts/patch_ava.py"),
        "--ava-root",
        str(ava_root),
    ]
    first = subprocess.run(command, check=True, capture_output=True, text=True)
    assert "server.py" in first.stdout

    module = _load_module(server_path, "patched_piper_server")

    class ModernPiper:
        modern_calls = 0
        legacy_calls = 0

        def synthesize_wav(self, text, wav_file):
            self.modern_calls += 1
            wav_file.writeframes(b"\x00\x00" * 160)

        def synthesize(self, *args, **kwargs):
            self.legacy_calls += 1
            raise AssertionError("legacy synthesize must not be called for modern Piper")

    voice = ModernPiper()
    audio = module.LocalAIServer(voice).render("Floodman test")
    assert len(audio) > 44
    assert voice.modern_calls == 1
    assert voice.legacy_calls == 0

    second = subprocess.run(command, check=True, capture_output=True, text=True)
    assert "already applied" in second.stdout


def test_patch_preserves_legacy_piper_call(project_root: Path, tmp_path: Path) -> None:
    ava_root = tmp_path / "ava"
    _write_http_fixtures(ava_root)
    server_path = ava_root / "local_ai_server/server.py"
    _write_server_fixture(server_path)

    subprocess.run(
        [
            sys.executable,
            str(project_root / "scripts/patch_ava.py"),
            "--ava-root",
            str(ava_root),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    module = _load_module(server_path, "patched_legacy_piper_server")

    class LegacyPiper:
        calls = 0

        def synthesize(self, text, wav_file):
            self.calls += 1
            wav_file.writeframes(b"\x00\x00" * 160)

    voice = LegacyPiper()
    audio = module.LocalAIServer(voice).render("Floodman test")
    assert len(audio) > 44
    assert voice.calls == 1
