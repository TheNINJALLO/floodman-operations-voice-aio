# Floodman Voice Appliance

A single-unit, self-hosted telephone receptionist with CPU inference by default. An NVIDIA RTX A1000 remains an optional CUDA profile.

## One server, one image

The image supervises three internal processes:

1. **llama.cpp** serving the pinned **Deckard 40B Q4_K_M** as the primary local model on CPU or an explicitly selected CUDA device.
2. **Floodman Voice Core** running Faster-Whisper, Kokoro, the deterministic intake state machine, transcripts, knowledge, team SMS, and the web panel.
3. **Asterisk** handling SIP, RTP, transfers, and the AudioSocket bridge.

The separate legacy unified image also supervises the Floodman Business Suite on ports
9000-9004. Voice records locally first and forwards signed, ordered call events
over loopback so Office, RoomFlow, estimates, tasks, and staff notifications use
the same call/customer workflow. Business services cannot delay or terminate a
live call.

Only the carrier is required externally. Twilio or another SIP provider supplies the phone number, minutes, and SMS. `LOCAL_AI_ONLY=true` is the default and ignores retained Deepgram, Groq, and ElevenLabs credentials. The Deckard primary profile always uses the local LLM even if older cloud LLM credentials remain configured. Optional cloud inference requires an explicit custom model profile, `LOCAL_AI_ONLY=false`, and provider credentials; unavailable providers retain the configured local completion fallback.

With `VOICE_ENGINE=tel_agent`, Tel-Agent owns the full-duplex call loop rather than only
the provider adapters: caller audio capture, streamed token-to-phrase playback,
barge-in, first-audio latency logging, and spoken-only transcript commits. Asterisk
remains the private media transport and Floodman remains the deterministic authority
for safety, routing, verification, customer records, and business actions.

The fully local profile transcribes endpointed utterances with Faster-Whisper. Continuous streaming recognition is available only with the optional external recognizer. The local energy detector uses configurable silence thresholds; it does not determine semantic sentence completion. Live-call testing is required to tune pauses and verify response times.

Optional conversational wording has a 1200 ms total budget (`TEL_AGENT_WORDING_BUDGET_MS`, bounded 200–3000 ms). A complete short rewrite must arrive within that budget; otherwise the existing approved response is spoken. Partial rewrites are buffered so a stalled model cannot leave the caller with half a question. This limits optional wording waits; it does not establish fast generative conversation on a 40B CPU model. `LLAMA_TIMEOUT_SECONDS` separately bounds complete extraction/knowledge requests, including recovery attempts.

## Reliability design

The application captures clear caller-provided details and chooses relevant questions without waiting for model extraction. A caller can give several details in one turn. Property type, location, timing, and cause are retained when explicitly provided; uncertain details remain unknown. Contact values are confirmed before being treated as confirmed intake data. Those confirmations are not identity verification or permission for protected billing actions.

The adaptive conversation flow:

1. Listen to the request and reuse details the caller already supplied. There is no default home-or-business question.
2. For a service request, ask up to two missing, relevant follow-ups without a routine hazard checklist. Volunteered emergency conditions still reach the deterministic emergency handler. For a general message, quote follow-up, or warranty-document request, use a shorter message-taking path.
3. Collect name and callback number first. Property requests also collect a service address and put email last. General messages do not require an address or email; warranty-document requests can collect email. Unknown or declined fields do not trap the caller in a loop.
4. Ask whether there is anything else to pass along. Save additions and continue listening until the caller clearly finishes; hesitations and unfinished sentences cannot close the call. A partial message remains marked partial when required contact details could not be confirmed.
5. End after the closing response. An interrupted farewell keeps the channel open for corrections; completion notifications are queued after the final farewell or disconnect, with local data saved first.

Prepared contextual questions and confirmations bypass optional wording generation, so a 40B CPU rewrite does not delay those turns. Deckard remains the primary local model for approved knowledge answers, eligible optional wording, and configured post-call briefing drafts. Warmed speech reduces generation work; actual speech recognition, synthesis, and carrier latency still require phone testing.

A caller asking for a moment hears a short acknowledgement. The next two no-input intervals remain quiet and preserve the active field. Normal no-input recovery resumes afterward. Both the native and Tel-Agent audio loops recognize this intentional wait without recording a missing-audio error.

Unsupported work is still collected and sent. A hangup sends whatever was recovered. Live transfers require a reported active flooding emergency and the configured emergency number. Standing water alone, past damage, and ordinary requests for a person collect a callback message. Volunteered immediate hazards still trigger a safety alert and advice without authorizing a live transfer. There is no routine hazard checklist.

Human handoff commits the transfer action before sending AudioSocket's explicit `0x00 0x00 0x00` termination packet. This avoids the Asterisk 20.6 behavior where a bare TCP EOF leaves the media application waiting instead of returning to the dialplan. AGI commands wait for Asterisk's acknowledgement before consuming the action file. The existing `TryExec` path then reaches `Dial`, with ringback, answered-transfer logging, and a spoken fallback if the destination cannot be reached. The CPU build runs a real Asterisk handoff check on isolated Local channels with networking disabled; it places no carrier calls.

## Local model profile

| Function | Local component | Device |
|---|---|---|
| Speech recognition | Faster-Whisper `small.en`, INT8 | CPU |
| Conversation, extraction, knowledge answers | Deckard 40B `Q4_K_M` through llama.cpp | CPU by default; optional CUDA |
| Voice | Kokoro ONNX, `af_heart` by default | CPU |
| Voice fallback | eSpeak NG | CPU |
| Knowledge | Approved Markdown + structured service area | CPU |
| Persistence | SQLite | NVMe/storage |

The default context is 4096 tokens and one inference lane. Begin with one simultaneous call. Extra RAM allows models to remain loaded; it does not establish response latency or concurrent-call capacity.

For the CPU host, use this initial profile in Pterodactyl Startup or `DATA_DIR/runtime.env`:

```dotenv
LOCAL_AI_DEVICE=cpu
LOCAL_AI_ONLY=true
PRIMARY_MODEL_PROFILE=deckard
VOICE_ENGINE=tel_agent
LLAMA_THREADS=6
LLAMA_BATCH_THREADS=6
FASTER_WHISPER_DEVICE=cpu
FASTER_WHISPER_COMPUTE_TYPE=int8
FASTER_WHISPER_THREADS=4
KOKORO_THREADS=2
NO_INPUT_TIMEOUT_SECONDS=12
INPUT_PREROLL_MS=240
```

These thread counts are starting budgets, not measured optima for a Ryzen host. Confirm the panel's CPU quota and affinity before increasing them. Kokoro uses a bounded ONNX CPU pool with idle spinning disabled so it leaves capacity for recognition and telephony. CPU mode always starts llama.cpp with zero GPU layers, including when an older egg retained `LLAMA_GPU_LAYERS=99`.

The no-input timeout measures silence across continuous media frames as well as missing media. The input preroll retains quiet word beginnings, and both playback paths preserve a caller who starts answering just before the assistant finishes.

## Primary Deckard model

The default `PRIMARY_MODEL_PROFILE=deckard` uses the exact Deckard 40B Q4 GGUF pinned in `next-project`. Its model identity, filename, publisher revision, 24,253,604,896-byte size, and SHA-256 are in `config/deckard-model.json`. Download, native launcher, application settings, and both voice drivers share this selection. Old `LLAMA_MODEL_REPO`, `LLAMA_MODEL_FILENAME`, `LLAMA_MODEL_PATH`, and `LLAMA_MODEL_ALIAS` panel values cannot silently retain the previous generic model while this profile is selected.

Deckard is the live model for streamed conversational wording, intake extraction, and approved-knowledge answers. Thinking is disabled in API parameters, responses stay bounded, and the resident server keeps the weights loaded between turns. Application code continues to decide routing, verification, compliance, transfers, and notifications. Model failures retain deterministic call recovery; there is no automatic substitution of generic Qwen. Another model requires `PRIMARY_MODEL_PROFILE=custom` plus explicit repository, filename, and alias.

First boot downloads the pinned GGUF into `DATA_DIR/models/llm` if necessary, checks size and SHA-256, then starts the native server. Existing weights can be reused with `DECKARD_MODEL_PATH`; a wrong or damaged file is rejected. Download and model loading happen before readiness. `/ready` reports the selected profile and alias, and readiness checks that the native server actually lists that alias. This is source/configuration validation, not proof of conversational quality: measure first audio and interruptions on the CPU host before carrier cutover.

Post-call briefings reuse the same resident Deckard service by default:

```dotenv
POST_CALL_BRIEFINGS_ENABLED=true
BRIEFING_BACKEND=primary
BRIEFING_TIMEOUT_SECONDS=120
```

No second Ollama process or duplicate model load is required in this mode. `LLAMA_THREADS` controls the shared native model's CPU budget. A separate private Ollama service remains an explicit alternative:

```dotenv
BRIEFING_BACKEND=ollama
BRIEFING_OLLAMA_URL=http://127.0.0.1:11434
BRIEFING_MODEL=floodman-deckard:40b-q4_k_m
BRIEFING_THREADS=4
BRIEFING_TIMEOUT_SECONDS=120
```

For that optional Ollama mode, loopback refers to the Voice container. The CRM's managed Ollama is bound to loopback inside its separate Billing container, so copying that address does not connect the containers. Private RFC1918/ULA addresses and known local Docker hostnames are supported; credentials in URLs, public IPs, redirects, environment proxies, and cloud model tags are rejected or disabled. That adapter does not download models and publishes no AI port. Enable it only after the intended private endpoint and installed tag are configured.

Completed/partial calls queue local SQLite jobs after their call records are saved. Managers may also choose **Generate briefing** on the call page. A single worker uses the configured CPU budget, thinking disabled, and a JSON schema. It starts only while Voice has no active calls and cancels its local HTTP work/defer-requeues the job when a call arrives. Cancellation is best effort at the model server; this does not establish resource isolation or coordinate the CRM's separate inference lock. Measure shared-host CPU contention before enabling production drafts. The primary native model remains resident; optional Ollama drafts use a five-minute keep-alive.

Drafts include numbered, exact transcript quotations. Invalid evidence, empty/incomplete output, timeouts, and connection failures are recorded as bounded error codes; interrupted jobs survive restart, and failures retry up to three times. Staff corrections invalidate old drafts; stale results are hidden until regenerated. AI wording still requires review against the original call. The draft cannot change intake, urgency, verified facts, recipients, consent, routing, or billing, and it sends no messages. Existing deterministic team alerts continue to link to the same call record.

The existing `BusinessSuiteBridge` is not compatible with next-project's current customer-inquiry API. Its webhook and HMAC contract differ, and the CRM's external inquiry webhook is disabled. Do not repoint the bridge, enable mock ingestion for real callers, or assume the portal is synchronized. A separate authenticated adapter and contract tests are needed before that connection can carry real calls.

Before replacing the existing provider, replay short yes/no answers, mid-sentence pauses, spelled contact information, interruptions, noise, hangups, and emergency/human transfers over the actual carrier. For Tel-Agent local turns, `first_audio_ms` includes transcription, business processing, model wording, and synthesis after audio capture finishes; add the configured endpoint silence to assess the caller's full wait. Check median and worst-case waits under the intended call load. Confirm completed and partial briefings reach exactly the selected members and inspect recorded delivery failures. The text simulator does not validate audio quality or latency.

## Quick deployment

1. Keep this source under `appliance/` in `TheNINJALLO/floodman-operations-voice-aio`.
2. For the standalone CPU deployment, build `Dockerfile.cpu`. The `deploy-deckard-cpu.yml` workflow tests and publishes an immutable `cpu-deckard-<commit>` tag; deploy its image digest. The historical `gpu-appliance` workflow builds the separate unified image.
3. For CPU operation, use `LOCAL_AI_DEVICE=cpu` and the ordinary Docker runtime. For CUDA operation, set `LOCAL_AI_DEVICE=cuda`, install the NVIDIA driver and Container Toolkit, and expose the GPU to the container.
4. Import `pterodactyl/egg-floodman-voice-appliance.json`.
5. Assign web port `8002`, SIP port `5060` and UDP RTP allocations `10000-10100`.
6. Fill in the SIP/Twilio and team-recipient values. Keep `LOCAL_AI_ONLY=true` for fully self-hosted AI.
7. Start the server. First boot downloads the approximately 24.3 GB Deckard GGUF plus the speech models into `/home/container/data/models`, unless existing verified weights are supplied. Allow disk space and startup time for download, verification, and model loading.
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
- Saved per-call timing/error timelines, problem reports, and manager diagnostic downloads

## Call troubleshooting inside the panel

Open **Calls**, select a call, and expand **Call diagnostics**. New phone calls automatically save listening and speech boundaries, recognition and processing duration, first response audio timing, interruptions, audio queue overflow, provider retries/fallbacks, and the call outcome. Each event has a call ID, UTC timestamp, turn number, and time from the start of that call's diagnostic session. The runtime snapshot includes model/provider names, voice, CPU affinity/quota and memory limit when available, thread budgets, endpoint settings, package versions, and an appliance code fingerprint. It excludes service credentials and raw provider error messages.

Choose **Report a problem**, select what went wrong, and optionally add the approximate second and what you heard. Feedback is saved with the call and marks it in the recent-call list. Managers and administrators can choose **Download diagnostics** to get one JSON file containing this call's timeline, feedback, existing transcript, recovered intake, and delivery history. Downloading and reporting are audited. All data remains in SQLite under `DATA_DIR`; deleting a call removes its timeline and feedback too.

The collector writes small batches in a background thread through a bounded queue. Diagnostic disk failures cannot block the audio loop. The download reports collector backlog and any discarded or failed events since process startup. Timelines are bounded in the panel and download; truncation is explicitly identified, and the original runtime snapshot is retained. Calls made before this feature was deployed have no retrospective timeline. Text simulations retain their transcripts but do not generate phone audio timings.

`CALL_SLOW_RESPONSE_MS=2000` marks a measured reply wait at or above two seconds as a diagnostic warning; it does not change speech, routing, or policy decisions. First audio means the first PCM frame written after caller capture finishes: local recognition and reply preparation are included, while endpoint silence and carrier playback buffering add time. These measurements and the recognized transcript do not establish exactly what the caller heard. This feature does not enable audio recording.

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

Narrative answers use `CONVERSATION_ENDPOINT_SILENCE_MS=600` as a minimum pause,
while yes/no confirmations retain the faster normal endpoint. An interrupted
company introduction is resumed instead of immediately asking about availability.
Unrecognized audio is distinguished from a full no-input interval. These local
timers and bounded fragment checks improve turn-taking; they are not a semantic
streaming speech model and require real telephone validation.

Email spelling uses its own `EMAIL_ENDPOINT_SILENCE_MS` so natural pauses do not
split an address into separate turns. `EMAIL_READBACK_SPEED` slows only the
spelled readback; the surrounding conversation retains the selected voice speed.
If a caller interrupts a newly asked question with the end of their previous
answer, Alex preserves that continuation and asks the interrupted question again.

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
