# Floodman Voice Appliance

A clean, single-unit, self-hosted telephone receptionist built for an **NVIDIA RTX A1000 8 GB**. It replaces the Groq, Deepgram, ElevenLabs, OpenAI, and AVA inference maze with one purpose-built appliance.

## One server, one image

The image supervises three internal processes:

1. **llama.cpp** serving Qwen3-4B Q4_K_M on the RTX A1000.
2. **Floodman Voice Core** running Faster-Whisper, Kokoro, the deterministic intake state machine, transcripts, knowledge, team SMS, and the web panel.
3. **Asterisk** handling SIP, RTP, transfers, and the AudioSocket bridge.

The production image also supervises the Floodman Business Suite on ports
9000-9004. Voice records locally first and forwards signed, ordered call events
over loopback so Office, RoomFlow, estimates, tasks, and staff notifications use
the same call/customer workflow. Business services cannot delay or terminate a
live call.

Only the carrier remains external to the local voice path. Twilio or another SIP provider supplies the phone number, minutes, and SMS. No paid AI key is accepted or required for calling.

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
9. Team SMS, permission-scoped email/browser notifications, and the 24-hour callback message

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
8. Open the web panel, expand **Set up the first administrator**, and use the generated or configured `ADMIN_TOKEN` once. Create a named administrator and use username/password access after that.

## Persistent layout

```text
/home/container/data/
├── floodman.db
├── runtime.env
├── knowledge/
├── service_area.yaml
├── models/
├── cache/tts/
├── push/
├── runtime/precall/
├── runtime/actions/
├── asterisk/
└── logs/
```

The startup environment parser validates the whole file before changing it. Broken quotation marks stop startup. Duplicate keys are reduced to one effective line. Existing Pterodactyl Startup values win over `runtime.env`. Only `ADMIN_TOKEN` and `INTERNAL_TOKEN` can be generated locally.

## Web panel

- Username/password accounts with administrator, manager, and viewer roles
- Call list, editable intake details, full transcripts, and controlled deletion
- Editable approved knowledge documents and service-area cities
- Live selection and preview of installed American and British English voices, with persisted speaking speed
- Per-user notification preferences, in-app notifications, and Web Push alerts
- Per-user email destinations, permission-scoped SMTP call alerts, provider testing, and channel delivery history
- Local conversation simulator, model readiness, audit history, and bounded log tails

The original `ADMIN_TOKEN` remains a short-lived recovery path and is not a day-to-day account. Passwords are salted and one-way hashed; changing a password revokes that user's sessions. Administrative changes are written to the audit log.

Browser push requires the public panel to use HTTPS. Each team member signs in on their own device, opens **Notifications**, and chooses **Enable alerts**. Permission is controlled by the browser and can be revoked there at any time. Push previews intentionally contain no caller name, phone number, address, or project details; those remain behind authenticated access.

Email delivery is configured by an administrator under **Email delivery**. SMTP passwords are persisted with restricted file permissions in `DATA_DIR/email-settings.json`, never returned to the browser, and can be bootstrapped from the `SMTP_*` environment variables. Each active user supplies a unique email address and chooses both the email channel and the call-event types they are permitted to receive. New-call messages contain only a secure portal link; completed, emergency, transfer, and partial-call emails contain the recovered intake summary and link to the authenticated call workspace. SMTP and SMS run outside the live conversation path.

Administrators can open **Voice**, choose any English voice installed in the Kokoro bundle, adjust the speed, and generate a local preview before saving. The selection is stored in `DATA_DIR/voice-settings.json` and takes precedence over the environment default after restart. The portal refuses to start voice previews or changes while it detects a live phone call.

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

Transfers are placed through the same trunk. Team SMS can use an Account SID/Auth Token or Twilio API key pair. Team email supports authenticated or trusted-relay SMTP with STARTTLS, direct TLS, or an explicitly selected unencrypted private relay.

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

### Natural turn-taking

Caller barge-in is enabled by default. While Alex is speaking, sustained caller
speech stops further playback and the complete buffered answer is sent to local
speech recognition. `BARGE_IN_MIN_SPEECH_MS`, `BARGE_IN_ENERGY_THRESHOLD`, and
`BARGE_IN_PREROLL_MS` can be tuned for unusually noisy telephone lines without
exposing AudioSocket or speech services publicly.

The repository tests deterministic intake, confirmations, unsupported work, emergency routing, account authorization, browser-notification privacy, service-area and knowledge editing, database cascades, environment parsing, Asterisk rendering, and the one-unit GPU contract.

## Production acceptance checklist

- RTX A1000 visible through `nvidia-smi` inside the container
- Qwen3 model loaded without VRAM exhaustion
- Faster-Whisper and Kokoro ready
- `/ready` returns HTTP 200
- First named administrator can sign in and the recovery token still works
- Browser push can be enabled on an HTTPS device and a test notification arrives
- Inbound and outbound RTP passes through AudioSocket
- Complete intake finishes in the simulator and by phone
- Partial hangup sends exactly one alert per recipient
- Human and emergency transfers work
- Backup and restore of `/home/container/data` tested

This release is a tested source build. The target GPU, carrier, and live audio path still require acceptance testing on the actual node.
