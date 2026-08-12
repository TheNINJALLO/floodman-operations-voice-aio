# Pterodactyl Deployment

## Import

Import:

```text
egg-floodman-operations-voice-aio-v1.1.1.json (or pterodactyl/egg-floodman-operations-voice-aio.json)
```

The egg expects a prebuilt image, normally:

```text
ghcr.io/theninjallo/floodman-operations-voice-aio:<release-tag>
```

Use a tagged image or immutable digest in production rather than `latest`.

## Critical network requirement

Pterodactyl is suitable only when the Wings node can expose SIP and RTP directly on a stable public IP. An HTTP reverse proxy, Cloudflare proxy, VPN-only route, or NAT-only shared web allocation is not enough.

Twilio must be able to reach:

- the SIP UDP allocation for the initial profile, normally `5060/UDP`, or
- the SIP TLS allocation for the secure profile, normally `5061/TCP`, and
- every UDP port in the configured RTP range.

The public IP placed in `PUBLIC_IP` must be the address Twilio sees and the address Asterisk advertises in SIP/SDP.

## Allocations

Recommended allocations on the same Pterodactyl server:

| Port | Protocol | Purpose |
|---|---|---|
| primary allocation | TCP | Dashboard, normally container port 9000 |
| 5060 | UDP | Initial Twilio SIP signaling |
| 5061 | TCP | Secure Twilio SIP signaling |
| 10000 through 10040 | UDP | RTP media |

The AudioSocket gate, ARI, AMI, AVA AudioSocket, and local-AI WebSocket remain internal and do not need allocations.

Pterodactyl must map the same port number externally and internally for SIP and every RTP port. Do not map a random external SIP port to a different internal port without also updating the Twilio origination URI and generated Asterisk configuration.

A larger RTP range permits more simultaneous calls and transfers. Update the egg variables, panel allocations, host firewall, and Twilio-facing firewall together.

## First boot

Start safely:

```dotenv
SIP_TRUNK_MODE=disabled
OUTBOUND_ENABLED=false
AMI_ENABLED=false
ROOMFLOW_ENABLED=false
STARTUP_PREFLIGHT=warn
PRINT_BOOTSTRAP_SECRETS=false
```

The first launch creates:

```text
/home/container/data/runtime.env
/home/container/data/config/floodman.yaml
/home/container/data/config/ava/ai-agent.local.yaml
/home/container/data/asterisk/
/home/container/data/ava/
/home/container/data/floodman-voice.sqlite3
```

Generated secrets are not printed by default. Retrieve only the admin token from the console:

```bash
grep '^export ADMIN_TOKEN=' /home/container/data/runtime.env
```

Do not delete `runtime.env` unless rotating all generated application, ARI, and AMI secrets intentionally.

## HTTPS

The dashboard and upload portal should use HTTPS. The primary Pterodactyl allocation can sit behind the panel operator’s HTTPS reverse proxy, but that proxy must expose only the dashboard port.

Do not proxy or expose:

- `/internal/*`
- ARI
- AMI
- AudioSocket
- local AI ports

Set:

```dotenv
PUBLIC_BASE_URL=https://voice.floodman.com
TRUSTED_HOSTS=voice.floodman.com,localhost,127.0.0.1
TRUSTED_PROXY_IPS=<exact reverse proxy IP or CIDR>
FORCE_HTTPS=true
ENABLE_API_DOCS=false
```

## Twilio runtime variables

Set the runtime fields in the egg:

```dotenv
SIP_TRUNK_MODE=twilio
PUBLIC_IP=<Wings node public IPv4>
LOCAL_NET=<container network CIDR>
SIP_PORT=5060
RTP_START=10000
RTP_END=10040
TWILIO_TERMINATION_URI=<unique-name>.pstn.ashburn.twilio.com
TWILIO_SIP_USERNAME=<SIP credential username>
TWILIO_SIP_PASSWORD=<SIP credential password>
TWILIO_PHONE_NUMBER=+1XXXXXXXXXX
TWILIO_FROM_NUMBER=+1XXXXXXXXXX
TWILIO_SECURE_TRUNKING=false
TWILIO_RTP_SYMMETRIC=false
```

Configure human transfer numbers and the Google DID in E.164 format.

## Provision Twilio outside Pterodactyl

Do not add the Twilio Account SID, API key secret, or Auth Token to egg variables.

Provision the trunk from a trusted workstation using the repository:

```bash
cp .env.twilio.example .env
cp .env.twilio-provisioning.example .env.twilio-provisioning
chmod 600 .env .env.twilio-provisioning

python3 scripts/preflight.py \
  --env-file .env \
  --env-file .env.twilio-provisioning \
  --strict

make twilio-plan
make twilio-apply
make twilio-verify
```

Alternatively, create the Elastic SIP Trunk, credential list, origination URI, and number association manually in Twilio Console. Copy only the resulting SIP runtime values into Pterodactyl.

## Preflight on the server

After adding the runtime variables, restart and run:

```bash
python /opt/floodman/scripts/preflight.py --strict
```

Check:

```text
/health
/readyz
```

The detailed diagnostics endpoint is available through the authenticated dashboard.

## Controlled test call

Add one operator-owned mobile number:

```dotenv
AMI_ENABLED=true
TEST_CALLS_ENABLED=true
TEST_CALL_ALLOWLIST=+1XXXXXXXXXX
```

Restart, open the dashboard, and place the operator echo call. Confirm that Asterisk returns the operator’s voice. Then set `TEST_CALLS_ENABLED=false` and restart.

If the phone rings but audio is missing, inspect the Wings node firewall, provider firewall, public SDP address, and RTP allocations. Do not try to fix an RTP problem with the dashboard reverse proxy.

## Secure trunking

For TLS and SRTP, upload the certificate and private key to persistent paths such as:

```text
/home/container/data/tls/fullchain.pem
/home/container/data/tls/privkey.pem
```

Set:

```dotenv
TWILIO_SECURE_TRUNKING=true
SIP_TLS_PORT=5061
SIP_TLS_CERT_FILE=/home/container/data/tls/fullchain.pem
SIP_TLS_KEY_FILE=/home/container/data/tls/privkey.pem
SIP_TLS_CA_FILE=/etc/ssl/certs/ca-certificates.crt
SIP_TLS_VERIFY_SERVER=true
```

Update Twilio’s origination URI to use `transport=tls`, permit `5061/TCP`, and repeat the entire test matrix.

## Local AI resources

CPU-only local voice is possible, but latency depends on selected AVA models. Begin with at least:

- 4 to 8 CPU threads
- 8 GB RAM for a light local profile
- 20 GB or more disk headroom
- no memory or disk overcommit that causes frequent throttling

For a first local model installation:

```dotenv
AUTO_INSTALL_LOCAL_MODELS=true
LOCAL_MODEL_TIER=LIGHT
```

After the model files are ready, return `AUTO_INSTALL_LOCAL_MODELS=false`.

A cloud or hybrid provider reduces local resource demand but requires provider credentials in persistent AVA configuration.

## Backup

Run:

```bash
python /opt/floodman/scripts/backup.py \
  --data-dir /home/container/data \
  --output-dir /home/container/data/backups
```

Download the resulting archive through SFTP and copy it to encrypted off-host storage. See [Backup and recovery](BACKUP_AND_RECOVERY.md).

## Updating

1. Pause new outbound campaigns.
2. Create and export an application backup.
3. Record the current image digest and release.
4. Select the new tagged image.
5. Start with `OUTBOUND_ENABLED=false`.
6. Run strict preflight and review diagnostics.
7. Complete an operator echo call and direct inbound call.
8. Test human fallback and transfers.
9. Resume Roomflow synchronization.
10. Resume outbound campaigns last.
