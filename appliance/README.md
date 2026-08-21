# Floodman Voice Appliance

A clean, single-unit, self-hosted telephone receptionist built for an **NVIDIA RTX A1000 8 GB**. It replaces the Groq, Deepgram, ElevenLabs, OpenAI, and AVA inference maze with one purpose-built appliance.

## One server, one image

The image supervises three internal processes:

1. **llama.cpp** serving Qwen3-4B Q4_K_M on the RTX A1000.
2. **Floodman Voice Core** running Faster-Whisper, Kokoro, the deterministic intake state machine, transcripts, knowledge, team SMS, and the web panel.
3. **Asterisk** handling SIP, RTP, transfers, and the AudioSocket bridge.

Only the carrier remains external. Twilio or another SIP provider supplies the phone number, minutes, and SMS. No paid AI key is accepted or required.

## Reliability design

The local model extracts structured information. The application owns every customer-facing question, confirmation, transition, notification, and call-ending decision. This prevents the model from inventing long acknowledgements or leaving the caller in dead air.

Intake order:

1. What happened and where
2. Home or business
3. When it began
4. Safety concerns
5. Name and one-field confirmation
6. Email and one-field confirmation
7. Phone and one-field confirmation
8. Address and one-field confirmation
9. Team SMS and the 24-hour callback message

Unsupported work is still collected and sent. A hangup sends whatever was recovered. Immediate electrical, sewage, rising-water, gas, or structural danger triggers emergency notification and an optional transfer.

## Local model profile

| Function | Local component | Device |
|---|---|---|
| Speech recognition | Faster-Whisper `small.en`, INT8 | CPU |
| Reasoning/extraction | Qwen3-4B `Q4_K_M` through llama.cpp | RTX A1000 |
| Voice | Kokoro ONNX, `af_heart` by default | CPU |
| Voice fallback | eSpeak NG | CPU |
| Knowledge | Approved Markdown + structured service area | CPU |
| Persistence | SQLite | NVMe/storage |

The default context is 4096 tokens and one inference lane. This is intentionally sized for one dependable live call on 8 GB VRAM rather than benchmark confetti.

## Quick deployment

1. Keep this source under `appliance/` in `TheNINJALLO/floodman-operations-voice-aio`.
2. Run the repository installer workflow. It publishes:
   `ghcr.io/theninjallo/floodman-operations-voice-aio:gpu-appliance` directly. A reusable future-build template remains at `ci/ci-gpu-appliance.yml`.
3. Install the NVIDIA driver and NVIDIA Container Toolkit on the Pterodactyl node. Configure Docker/Wings so the container receives the GPU.
4. Import `pterodactyl/egg-floodman-voice-appliance.json`.
5. Assign web port `8003`, SIP port `5060` and UDP RTP allocations `10000-10100`.
6. Fill in the SIP/Twilio and team-recipient values. AI provider keys do not exist in this project.
7. Start the server. First boot downloads roughly several gigabytes of local model files into `/home/container/data/models`.
8. Open the web panel with the generated or configured `ADMIN_TOKEN` and run the conversation simulator before placing the first phone call.

## Persistent layout

```text
/home/container/data/
├── floodman.db
├── runtime.env
├── knowledge/
├── service_area.yaml
├── models/
├── cache/tts/
├── runtime/precall/
├── runtime/actions/
├── asterisk/
└── logs/
```

The startup environment parser validates the whole file before changing it. Broken quotation marks stop startup. Duplicate keys are reduced to one effective line. Existing Pterodactyl Startup values win over `runtime.env`. Only `ADMIN_TOKEN` and `INTERNAL_TOKEN` can be generated locally.

## Web panel

- Call list and completed/partial intake
- Full caller and assistant transcript
- Notification status
- Local conversation simulator
- Model and readiness status
- Bounded diagnostic log tails

Health endpoints:

```text
GET /health
GET /ready
```

## Carrier configuration

The appliance supports IP-authenticated and registration trunks. Core fields are:

```text
SIP_SERVER
SIP_USERNAME
SIP_PASSWORD
SIP_FROM_USER
SIP_FROM_DOMAIN
SIP_MATCH_ADDRESSES
SIP_OUTBOUND_PROXY
PUBLIC_IP
TWILIO_PHONE_NUMBER
```

Transfers are placed through the same trunk. Team SMS can use an Account SID/Auth Token or Twilio API key pair.

## Migration from Floodman Operations Voice AIO

Do not overwrite the old live server. The current AIO repository builds this appliance as a separate image tag so it can be tested side-by-side. Copy approved knowledge and preserve the old database/recordings for audit:

```bash
python scripts/migrate_from_voice_aio.py /path/to/old/data --new-data /home/container/data
```

The new database schema is deliberately smaller and purpose-built. Historical databases are preserved rather than silently mutated.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

The repository tests deterministic intake, confirmations, unsupported work, emergency routing, service-area checks, partial notifications, database cascades, environment parsing, Asterisk rendering, and the one-unit GPU contract.

## Production acceptance checklist

- RTX A1000 visible through `nvidia-smi` inside the container
- Qwen3 model loaded without VRAM exhaustion
- Faster-Whisper and Kokoro ready
- `/ready` returns HTTP 200
- Inbound and outbound RTP passes through AudioSocket
- Complete intake finishes in the simulator and by phone
- Partial hangup sends exactly one alert per recipient
- Human and emergency transfers work
- Backup and restore of `/home/container/data` tested

This release is a tested source build. The target GPU, carrier, and live audio path still require acceptance testing on the actual node.
