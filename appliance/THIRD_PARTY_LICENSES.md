# Third-Party Components

This repository does not redistribute model weights. First boot downloads them from their upstream sources.

- Tel-Agent commit `8a5193b2d801714e45ae6981a3506a60d562a99e`: AGPL-3.0-or-later. The complete pinned source and license are retained at `/opt/tel-agent`; LiveKit and the optional `voice` extra are not installed.

- llama.cpp: MIT license
- Qwen3 model family: Apache 2.0 model license
- Faster-Whisper: MIT license
- CTranslate2: MIT license
- Kokoro-82M / kokoro-onnx: Apache 2.0 / project-specific upstream components; review upstream notices with every model update
- Asterisk: GPLv2 and bundled component licenses
- FastAPI, Uvicorn, HTTPX, PyYAML, NumPy, SciPy, SoundFile, ONNX Runtime, Hugging Face Hub: respective upstream open-source licenses

The operator remains responsible for carrier terms, call-recording law, privacy notices, model-license review, and any deployment-specific compliance.
