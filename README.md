# Floodman Operations Voice AIO v1.1.1

Floodman Operations Voice AIO is a production-ready self-hosted voice stack for Floodman that combines **Asterisk**, **AVA AI**, the **Floodman operations app**, **Twilio Elastic SIP Trunking**, a **web dashboard**, a **Google-aware call gate**, **Roomflow** integration hooks, and **Pterodactyl** deployment assets.

The repository is prepared for controlled inbound rollout first. Outbound dialing, AMI-driven automation, and external system sync stay behind explicit safety switches until infrastructure and live-call validation are complete.

## Project overview

Core components:

- **Asterisk** for SIP, RTP, ARI, AMI, transfers, and telephony control
- **AVA AI** for conversational handling and agent logic
- **Floodman app** for dashboard APIs, diagnostics, uploads, ledgering, and business workflows
- **Twilio** runtime support for Elastic SIP Trunking
- **Call gate** logic that waits through Google announcements and protects first-customer audio
- **Roomflow** local-first integration with retry-safe synchronization hooks
- **Pterodactyl eggs** for panel deployment

## Container images

### Full image

The full image includes the heavier bundled local-model packages:

- `faster-whisper`
- `vosk`
- `piper-tts`
- `llama-cpp-python`

### Lite image

The lite image is a real deployment subset, not a demo. It still includes:

- Asterisk
- AVA source and compatibility patching
- Floodman app and dashboard
- Twilio runtime support
- Call gate / Whisper classifier
- Roomflow support

It omits the heaviest local-model dependencies so externally configured or cloud-assisted AVA deployments can run with a smaller image footprint.

## GitHub build process

The publishing workflow is **`CI and Publish Floodman Containers`** in `.github/workflows/ci-container.yml`.

### Triggers

- Push to `main`
- Push of tags matching `v*`
- Manual `workflow_dispatch`

### Build order

1. Install Python dependencies
2. Run `pytest`
3. Run Python compilation validation
4. Validate shell scripts and configuration
5. Build and push the **full** image
6. Build and push the **lite** image

### Published GHCR tags

| Event | Image | Published tag |
| --- | --- | --- |
| Push to `main` | Full | `ghcr.io/theninjallo/floodman-operations-voice-aio:latest` |
| Push to `main` | Lite | `ghcr.io/theninjallo/floodman-operations-voice-aio:lite` |
| Push tag `v1.1.1` | Full | `ghcr.io/theninjallo/floodman-operations-voice-aio:v1.1.1` |
| Push tag `v1.1.1` | Lite | `ghcr.io/theninjallo/floodman-operations-voice-aio:v1.1.1-lite` |

To publish the versioned release tags:

```bash
git tag -a v1.1.1 -m "Floodman Operations Voice AIO v1.1.1"
git push origin v1.1.1
```

## GHCR visibility

GitHub Container Registry packages are private on first publish unless explicitly changed.

Manual GitHub step required if Pterodactyl or anonymous pulls must work:

1. Open the repository owner's **Packages** page
2. Select `floodman-operations-voice-aio`
3. Open **Package settings**
4. Change visibility to **Public**
5. Confirm repository access settings are correct

## Pterodactyl deployment

Egg files:

- `egg-floodman-operations-voice-aio-v1.1.1.json`
- `pterodactyl/egg-floodman-operations-voice-aio.json`

### 1. Import the egg

In Pterodactyl Admin, go to **Nests** and import one of the egg JSON files.

### 2. Create the server

Recommended starting image:

- **Floodman AIO v1.1.1-lite (recommended)**
- `ghcr.io/theninjallo/floodman-operations-voice-aio:v1.1.1-lite`

Use the provided startup command:

```bash
bash /opt/floodman/scripts/entrypoint.sh
```

### 3. Assign required ports

| Purpose | Protocol | Port(s) |
| --- | --- | --- |
| Dashboard | TCP | `9000` |
| SIP | UDP | `5060` |
| SIP/TLS | TCP | `5061` |
| RTP media | UDP | `10000-10040` |

Make the dashboard allocation the primary allocation. Add SIP and the entire RTP UDP range before first live carrier testing.

### 4. Configure safe first boot settings

```dotenv
OUTBOUND_ENABLED=false
TEST_CALLS_ENABLED=false
AMI_ENABLED=false
ROOMFLOW_ENABLED=false
SIP_TRUNK_MODE=disabled
STARTUP_PREFLIGHT=warn
```

Keep real carrier credentials, real DIDs, and public IP values blank until networking and allocations are verified.

### 5. Start the server

On first boot the container generates runtime secrets and stores them in:

```text
/home/container/data/runtime.env
```

Retrieve `ADMIN_TOKEN` from that file after startup.

## Safe first boot checklist

Before any carrier cutover:

1. Confirm dashboard routing and firewall access
2. Confirm TCP 9000 responds
3. Confirm UDP 5060 and UDP 10000-10040 are reachable where applicable
4. Keep outbound and Roomflow disabled
5. Keep AMI disabled
6. Keep startup preflight on `warn` or `strict`
7. Verify `/livez` and `/readyz`

## Twilio testing order

Use this exact 14-step order:

1. Confirm the container passes `/livez`
2. Confirm `/readyz` reports the expected dependencies
3. Log in to the dashboard with `ADMIN_TOKEN`
4. Fill runtime `.env` values with the real public domain, public IP, and transfer destinations
5. Fill `.env.twilio-provisioning` on a trusted workstation only
6. Run preflight validation before any Twilio routing change
7. Run Twilio `show-config` and review the expected trunk settings
8. Run Twilio `plan`
9. Run Twilio `apply`
10. Run Twilio `verify`
11. Perform Twilio carrier playback / test-number validation on the trunk
12. Enable exactly one allowlisted echo test and verify two-way audio
13. Test direct inbound DID calls plus reception, emergency, billing, and estimating transfers
14. Test AVA direct calls first, then Google-forwarded calls, then secure SIP/TLS, then Roomflow, and only after all of that consider outbound enablement

## Configuration reference

### Core web and security

| Variable | Purpose | Recommended / safe value |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | External dashboard URL | real HTTPS URL |
| `TRUSTED_HOSTS` | Allowed host headers | include dashboard hostname plus localhost |
| `TRUSTED_PROXY_IPS` | Forwarded-header trust list | `127.0.0.1` unless a real proxy is present |
| `FORCE_HTTPS` | HTTPS redirect enforcement | `false` until proxy headers are trusted |
| `ENABLE_API_DOCS` | Swagger / OpenAPI exposure | `false` |
| `STARTUP_PREFLIGHT` | Startup enforcement level | `warn` initially, `strict` for hardened rollout |
| `PRINT_BOOTSTRAP_SECRETS` | Print runtime-generated secrets | `false` |

### Telephony and carrier

| Variable | Purpose | Safe first value |
| --- | --- | --- |
| `SIP_TRUNK_MODE` | Carrier mode | `disabled` |
| `SIP_PORT` | Public SIP UDP port | `5060` |
| `SIP_TLS_PORT` | Public SIP/TLS TCP port | `5061` |
| `RTP_START` | First RTP port | `10000` |
| `RTP_END` | Last RTP port | `10040` |
| `PUBLIC_IP` | Advertised public media/signaling IP | blank until known |
| `TWILIO_TERMINATION_URI` | Twilio termination hostname | blank |
| `TWILIO_SIP_USERNAME` | Twilio credential-list username | blank |
| `TWILIO_SIP_PASSWORD` | Twilio SIP digest password | blank |
| `TWILIO_PHONE_NUMBER` | Main DID | blank |
| `TWILIO_FROM_NUMBER` | Outbound caller ID | blank |
| `TWILIO_SECURE_TRUNKING` | Enable TLS/SRTP profile | `false` until UDP testing passes |
| `TWILIO_RTP_SYMMETRIC` | NAT fallback | `false` unless specifically required |

### Operational safety switches

| Variable | Purpose | Safe default |
| --- | --- | --- |
| `OUTBOUND_ENABLED` | Automated outbound dialing | `false` |
| `TEST_CALLS_ENABLED` | Controlled echo testing | `false` |
| `AMI_ENABLED` | Asterisk Manager Interface use | `false` |
| `ROOMFLOW_ENABLED` | Remote Roomflow synchronization | `false` |
| `AUTO_INSTALL_LOCAL_MODELS` | AVA local-model auto-download | `false` |
| `ENABLE_CARRIER_TEST_EXTENSIONS` | Twilio play-test dialplan helpers | `false` |

### Business routing

| Variable | Purpose |
| --- | --- |
| `FLOODMAN_LIVE_NUMBER` | Human reception transfer |
| `FLOODMAN_EMERGENCY_NUMBER` | Emergency escalation |
| `FLOODMAN_BILLING_NUMBER` | Billing transfer |
| `FLOODMAN_ESTIMATING_NUMBER` | Estimating transfer |
| `OUTBOUND_CALLER_ID_NUMBER` | Approved outbound caller ID |

### Persistent data

Mutable runtime state is stored under:

```text
/home/container/data
```

Important contents include:

- `runtime.env`
- SQLite databases
- uploads
- recordings
- model cache
- AVA state
- backups

## Local validation

```bash
pip install -e '.[dev]'
python3 -m compileall app/ scripts/ tests/
python3 -m pytest tests/ -x -q
python3 scripts/validate_configs.py
```

## Notes

- The container health check uses `GET /livez`
- Real `.env` files, databases, recordings, uploads, and private keys must never be committed
- Twilio provisioning credentials should stay off the long-running container
- The lite image is appropriate for first GHCR/Pterodactyl rollout when local LLM packaging is not required


<!-- FLOODMAN_KNOWLEDGE_LIBRARY -->
## Approved website knowledge library

Floodman public answers are grounded through two persistent layers:

- `data/config/floodman.yaml` for structured services, policies, and the published service area
- `data/knowledge/managed` and `data/knowledge/custom` for detailed approved Markdown

The managed August 12, 2026 pack was built from Floodman.com. Only documents with `approved: true`
are searchable. The local search returns excerpts and provenance to AVA and does not browse the
internet or learn from callers during a call. Custom operator documents survive managed-pack updates.
See `docs/KNOWLEDGE_LIBRARY.md` and `docs/WEBSITE_CONTENT_AUDIT.md`.
