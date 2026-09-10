# Unified Business Suite 4.7.3

- Embed the immutable Floodman Business Suite 4.7.3 image and checksum-verified runtime.
- Simplify the daily Business Suite pages and keep secondary applications under More tools.
- Require customer selection before listing properties and enforce cross-customer isolation.
- Preserve the Voice AIO call path, persistent data, notifications, owner account, and rollback image.

# v0.1.1

- Expose `/opt/venv/bin/python` as the container runtime.
- Use the virtualenv Python explicitly during startup.
- Validate the final image before publishing.
- Fix Pterodactyl exit 127: `python: command not found`.

# v0.1.0

Initial clean-room Floodman Voice Appliance release.

- One CUDA image and one Pterodactyl server
- Qwen3-4B Q4_K_M on RTX A1000 via pinned llama.cpp
- Faster-Whisper CPU STT
- Kokoro CPU TTS with eSpeak fallback
- Purpose-built AudioSocket call core without AVA
- Deterministic one-field intake and confirmations
- Approved local knowledge and 148 published Michigan communities
- Full and partial intake persistence
- Twilio team SMS with idempotency
- Human and emergency transfers
- Responsive web panel and browser simulator
- Quiet Asterisk module profile and file-based diagnostics
- Fail-closed, deduplicating runtime.env parser
- GitHub CUDA image build and Pterodactyl egg
