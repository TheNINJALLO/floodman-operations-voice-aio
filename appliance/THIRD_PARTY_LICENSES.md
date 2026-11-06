# Third-Party Components

This repository does not redistribute model weights. First boot downloads them from their upstream sources.

- Tel-Agent commit `8a5193b2d801714e45ae6981a3506a60d562a99e`: AGPL-3.0-or-later. The complete pinned source and license are retained at `/opt/tel-agent`; LiveKit and the optional `voice` extra are not installed.

- llama.cpp: MIT license
- Qwen3 model family: Apache 2.0 model license
- Faster-Whisper: MIT license
- CTranslate2: MIT license
- Kokoro-82M / kokoro-onnx: Apache 2.0 / project-specific upstream components; review upstream notices with every model update
- Optional local streaming trial: Sherpa-ONNX and the pinned English Zipformer model by k2-fsa/csukuangfj (Apache-2.0); Silero VAD by Silero/snakers4 (MIT).
- Optional Pocket TTS: code by Kyutai (MIT), pretrained model and voice states from [kyutai/pocket-tts-without-voice-cloning](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning) (CC-BY-4.0). Attribution: Kyutai, Pocket TTS, English model, revision `a7056f505a4a8ca72d8b5fb1440071d6f1a35739`. The generated local configuration changes only asset paths; weights are unchanged. No voice-cloning weights are used by this profile.
- Optional 8B GGUF: quantization by [mradermacher](https://huggingface.co/mradermacher/oh-dcft-v3.1-claude-3-5-haiku-20241022-i1-GGUF), upstream mlfoundations-dev, Llama 3.1 license as identified by the publisher. This is a Llama-family model, not Anthropic Claude weights. Identity, revision and SHA-256 are in `config/haiku8b-model.json`; weights are downloaded, not redistributed in the image.
- Optional CPU PyTorch 2.8.0 retains its upstream license and bundled notices.
- Asterisk: GPLv2 and bundled component licenses
- FastAPI, Uvicorn, HTTPX, PyYAML, NumPy, SciPy, SoundFile, ONNX Runtime, Hugging Face Hub: respective upstream open-source licenses

The operator remains responsible for carrier terms, call-recording law, privacy notices, model-license review, and any deployment-specific compliance.
# Expanded English voice presets

The 19 selectable Pocket TTS voices and their source attribution are listed in
`docs/VOICE_CATALOG.md`. Alba MacKenna and VCTK samples use CC BY 4.0; Unmute
voice donations and Voice-Zero samples use CC0. Source voice states are pinned
by revision and SHA-256 in `config/local-streaming-assets.json`.
