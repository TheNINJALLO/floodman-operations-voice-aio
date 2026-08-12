# Twilio Elastic SIP Trunking Setup

This guide connects a Twilio voice number directly to Floodman Operations Voice AIO:

```text
PSTN or Google forwarding
    -> Twilio Elastic SIP Trunk
    -> public SIP address
    -> embedded Asterisk
    -> Floodman Call Gate
    -> AVA
    -> Roomflow or local ledger
```

Twilio Elastic SIP Trunking does not register to Asterisk. Floodman sends outbound calls to the configured Twilio termination FQDN using digest credentials. Twilio sends inbound calls from its signaling networks to the configured public origination SIP URI.

## 1. Host and network requirements

Use a host with:

- A static, globally routable public IPv4 address
- Direct inbound UDP and TCP access
- Docker Engine and Docker Compose, or a Pterodactyl node with equivalent allocations
- A dashboard DNS name pointing to the host
- No carrier-grade NAT between Twilio and Asterisk

Required public ports:

| Port | Protocol | Purpose |
|---|---|---|
| 80 | TCP | Caddy certificate enrollment and HTTP redirect |
| 443 | TCP and UDP | Dashboard, upload portal, HTTP/3 where available |
| 5060 | UDP | Initial Twilio SIP profile |
| 5061 | TCP | Secure SIP profile after TLS is enabled |
| 10000 through 10040 | UDP | Asterisk RTP media |

Never expose:

- `5038/TCP` AMI
- `8088/TCP` ARI
- `9019/TCP` call-gate AudioSocket
- AVA internal AudioSocket ports
- the local AI WebSocket
- SQLite files

Twilio’s current Elastic SIP Trunking networking documentation lists all regional signaling ranges and the global media range `168.86.128.0/18`. Permit Twilio signaling ranges to the active SIP port and permit the media range to the configured RTP range. Review the current Twilio networking page before every firewall change because provider ranges can change.

### Example UFW policy

Adjust management networks and SSH before applying anything:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp

sudo ufw allow proto udp from 54.172.60.0/30 to any port 5060
sudo ufw allow proto udp from 54.244.51.0/30 to any port 5060
sudo ufw allow proto udp from 54.171.127.192/30 to any port 5060
sudo ufw allow proto udp from 35.156.191.128/30 to any port 5060
sudo ufw allow proto udp from 54.65.63.192/30 to any port 5060
sudo ufw allow proto udp from 54.169.127.128/30 to any port 5060
sudo ufw allow proto udp from 54.252.254.64/30 to any port 5060
sudo ufw allow proto udp from 177.71.206.192/30 to any port 5060
sudo ufw allow proto udp from 168.86.128.0/18 to any port 10000:10040

sudo ufw enable
```

For secure trunking, permit the same signaling ranges to `5061/TCP` and remove `5060/UDP` only after the TLS profile passes live tests.

## 2. Create the runtime file

```bash
cp .env.twilio.example .env
chmod 600 .env
```

Required values:

```dotenv
ENVIRONMENT=production
VOICE_DOMAIN=voice.floodman.com
PUBLIC_BASE_URL=https://voice.floodman.com
TRUSTED_HOSTS=voice.floodman.com,localhost,127.0.0.1
TRUSTED_PROXY_IPS=127.0.0.1,172.16.0.0/12
FORCE_HTTPS=false
ENABLE_API_DOCS=false
PRINT_BOOTSTRAP_SECRETS=false
STARTUP_PREFLIGHT=warn

SIP_TRUNK_MODE=twilio
PUBLIC_IP=<real public IPv4>
LOCAL_NET=172.16.0.0/12
SIP_PORT=5060
RTP_START=10000
RTP_END=10040
SIP_CODECS=ulaw

TWILIO_TERMINATION_URI=<unique-name>.pstn.ashburn.twilio.com
TWILIO_SIP_USERNAME=<strong username>
TWILIO_SIP_PASSWORD=<strong password>
TWILIO_PHONE_NUMBER=+1XXXXXXXXXX
TWILIO_FROM_NUMBER=+1XXXXXXXXXX
TWILIO_SECURE_TRUNKING=false
TWILIO_RTP_SYMMETRIC=false

OUTBOUND_CALLER_ID_NUMBER=+1XXXXXXXXXX
FLOODMAN_LIVE_NUMBER=+1XXXXXXXXXX
FLOODMAN_EMERGENCY_NUMBER=+1XXXXXXXXXX
FLOODMAN_BILLING_NUMBER=+1XXXXXXXXXX
FLOODMAN_ESTIMATING_NUMBER=+1XXXXXXXXXX
GOOGLE_DIDS=+1XXXXXXXXXX

AMI_ENABLED=true
TEST_CALLS_ENABLED=false
TEST_CALL_ALLOWLIST=+1<operator-mobile>
OUTBOUND_ENABLED=false
ROOMFLOW_ENABLED=false
```

Use E.164 numbers with the leading `+`.

`TWILIO_RTP_SYMMETRIC=false` is the secure default because Asterisk advertises the configured public address in SDP. Enable symmetric RTP only after proving that a specific NAT path requires it.

## 3. Create the provisioning file

```bash
cp .env.twilio-provisioning.example .env.twilio-provisioning
chmod 600 .env.twilio-provisioning
```

Use a restricted API key when possible:

```dotenv
TWILIO_API_REGION=us1
TWILIO_ACCOUNT_SID=AC...
TWILIO_API_KEY=SK...
TWILIO_API_KEY_SECRET=...
TWILIO_AUTH_TOKEN=

TWILIO_TRUNK_FRIENDLY_NAME=Floodman Operations Voice
TWILIO_TRUNK_DOMAIN=<unique-name>.pstn.twilio.com
TWILIO_ORIGINATION_FRIENDLY_NAME=Floodman Primary Asterisk
TWILIO_ORIGINATION_SIP_URI=sip:<public-ip>:5060;edge=ashburn
TWILIO_ORIGINATION_PRIORITY=10
TWILIO_ORIGINATION_WEIGHT=10
TWILIO_TRANSFER_MODE=disable-all
TWILIO_ALLOW_PHONE_ROUTING_CHANGE=false
TWILIO_ROTATE_SIP_PASSWORD_ON_APPLY=false
```

The base trunk domain must match the localized runtime termination URI:

```text
Runtime:       floodman-example.pstn.ashburn.twilio.com
Provisioning:  floodman-example.pstn.twilio.com
```

The provisioning script automates US1 trunk resources. Configure non-US regional trunk resources manually in Twilio Console.

## 4. Validate without changing anything

```bash
make validate
make test

python3 scripts/preflight.py \
  --env-file .env \
  --env-file .env.twilio-provisioning \
  --require-provisioning \
  --strict

make twilio-config
make twilio-plan
```

Strict preflight checks:

- HTTPS public URL
- strong application and SIP secrets
- host and proxy allowlists
- globally routable public IP
- RTP range size and bounds
- E.164 numbers
- origination URI host, port, edge, and TLS alignment
- trunk-domain alignment
- Twilio termination DNS when network checks are enabled

## 5. Apply Twilio resources

```bash
make twilio-apply
make twilio-verify
```

The script creates or reconciles:

- Elastic SIP trunk
- credential list
- SIP credential
- credential-to-trunk association
- origination URL
- number-to-trunk association

The script never prints the API secret or SIP password. It writes non-secret resource identifiers to:

```text
data/twilio/provisioning.json
```

### Existing phone routing guard

Associating a Twilio number with a trunk can replace its existing voice webhook or application routing. The provisioning tool inspects the number and refuses the change when it detects another route.

Only after reviewing that route should you temporarily set:

```dotenv
TWILIO_ALLOW_PHONE_ROUTING_CHANGE=true
```

Run `plan` again, apply intentionally, then return the setting to `false`.

### SIP password rotation

Twilio does not return existing credential passwords. Ordinary verification therefore validates the credential identity but does not pretend to compare the password.

For an intentional rotation:

```dotenv
TWILIO_ROTATE_SIP_PASSWORD_ON_APPLY=true
```

Run `apply`, test calls, then return the setting to `false`.

## 6. Start the service

With Caddy HTTPS:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.caddy.yml \
  up -d --build
```

Check service state:

```bash
docker compose ps
docker compose logs -f floodman-voice
```

Retrieve the admin token:

```bash
docker compose exec floodman-voice \
  sh -lc "grep '^export ADMIN_TOKEN=' /home/container/data/runtime.env"
```

Do not set `PRINT_BOOTSTRAP_SECRETS=true` on a shared console.

## 7. Runtime diagnostics

Public minimal probes:

```bash
curl -fsS https://voice.floodman.com/livez
curl -fsS https://voice.floodman.com/health
curl -fsS https://voice.floodman.com/readyz
```

Authenticated diagnostics:

```bash
ADMIN_TOKEN='<token>'
curl -fsS \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  https://voice.floodman.com/api/v1/diagnostics
```

A ready live-call stack should report:

- database ready
- call gate ready
- Asterisk ARI authenticated
- AVA Stasis application registered
- AMI ready when enabled
- Twilio DNS ready
- public configuration checks passing

The container-level strict preflight reads the runtime environment already loaded by the entrypoint:

```bash
docker compose exec floodman-voice \
  python /opt/floodman/scripts/preflight.py --strict
```

## 8. Test termination from Asterisk to Twilio

Twilio provides free `Play` test numbers that record and play back audio. They prove outbound signaling, authentication, and two-way media without reaching a customer.

Set temporarily:

```dotenv
ENABLE_CARRIER_TEST_EXTENSIONS=true
```

Restart and originate extension `8990` or `8991` from an internal Asterisk channel. The generated destinations are:

```text
+16504894546
+14154758378
```

Successful playback in both directions confirms the termination path.

## 9. Test Twilio origination to Asterisk

In Twilio Console, open the trunk’s test page, select the associated number, and use **Make a test call**.

Expected path:

```text
Twilio Play -> Floodman DID -> Asterisk -> Call Gate -> AVA or failover
```

For the first origination test, it is acceptable to route to the normal inbound path. The important observations are:

- Asterisk receives the INVITE
- the DID is recovered from the request or `Diversion` header
- the call is answered
- audio works in both directions
- the call gate does not trap the channel
- AVA registers in ARI or the human fallback route answers

## 10. Controlled operator echo call

This is the preferred end-to-end network test because it uses the actual Twilio trunk and an ordinary mobile phone but does not depend on STT, an LLM, or TTS.

Set only your operator-owned number:

```dotenv
AMI_ENABLED=true
TEST_CALLS_ENABLED=true
TEST_CALL_ALLOWLIST=+1<operator-mobile>
```

Restart the service. In the dashboard, place an outbound test call to the allowlisted number.

Expected sequence:

1. The mobile phone rings.
2. The operator answers.
3. Asterisk plays the echo-test prompt.
4. The operator speaks.
5. The operator hears the same speech returned.

Disable `TEST_CALLS_ENABLED` immediately after testing.

## 11. Direct inbound customer simulation

From at least two cellular carriers:

- Call the temporary Twilio DID.
- Begin speaking immediately on one call.
- Remain silent on another call.
- Interrupt the agent while it speaks.
- Ask for a person.
- Trigger each transfer destination.
- Disconnect during intake and confirm a usable call record remains.
- Stop Roomflow and confirm local storage plus an outbox entry.
- Stop the selected AI provider and confirm the human fallback route.

The immediate-speech call must preserve the opening request. The silent call should receive the normal greeting after the configured short delay.

## 12. AVA test

The echo test does not prove the AVA conversational pipeline. For a real AI call, configure either local models or a cloud provider.

Local proof profile:

```dotenv
AVA_PROVIDER=local_hybrid
AVA_PIPELINE=local_hybrid
ENABLE_LOCAL_AI_SERVER=true
AUTO_INSTALL_LOCAL_MODELS=true
LOCAL_MODEL_TIER=LIGHT
```

Restart and monitor model setup. When complete, set `AUTO_INSTALL_LOCAL_MODELS=false` and restart again.

Before moving forward, diagnostics must show:

```text
ava_stasis_application: ready
```

Then place a direct inbound call and complete:

- new inspection intake
- emergency intake
- existing-customer lookup
- human transfer
- callback task
- schedule lookup and appointment creation

## 13. Google-forwarded test

Do not tune the Google gate from synthetic text alone. Use a real Google forwarding path after direct calls are stable.

Validate:

- no greeting over the Google announcement
- no greeting loop
- live customer detected after the announcement
- customer’s first sentence preserved
- source tagged as Google where evidence supports it
- ordinary inbound route used when confidence is low
- automated Google business-information calls receive only approved public answers
- credential or payment requests route to the security policy

Keep the primary Floodman number on its existing route while this runs in shadow or temporary-DID mode.

## 14. Human transfer tests

Test every destination during business hours and after hours:

- reception
- emergency on-call
- billing
- estimating

Confirm:

- caller ID presentation
- ring duration
- no-answer behavior
- busy behavior
- voicemail behavior
- caller audio before and after bridge
- no duplicate outbound carrier leg
- AVA disconnect after a completed transfer

## 15. Secure trunking

After UDP/RTP is stable:

1. Install a valid certificate and key under `secrets/sip/`.
2. Copy `.env.twilio-secure.example` to `.env`.
3. Set:

```dotenv
TWILIO_SECURE_TRUNKING=true
SIP_TLS_PORT=5061
SIP_TLS_CERT_FILE=/run/secrets/floodman-sip/fullchain.pem
SIP_TLS_KEY_FILE=/run/secrets/floodman-sip/privkey.pem
SIP_TLS_CA_FILE=/etc/ssl/certs/ca-certificates.crt
SIP_TLS_VERIFY_SERVER=true
```

4. Change the origination URI:

```dotenv
TWILIO_ORIGINATION_SIP_URI=sip:<public-ip>:5061;transport=tls;edge=ashburn
```

5. Run strict preflight, plan, apply, and verify.
6. Start with all overlays:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.caddy.yml \
  -f docker-compose.twilio-secure.yml \
  up -d --build
```

7. Repeat termination, origination, echo, AI, and transfer tests.
8. Remove the UDP SIP firewall rule only after the secure path is confirmed.

## 16. Troubleshooting map

| Symptom | Most likely area |
|---|---|
| No ring and immediate failure | E.164 formatting, Twilio credential, trunk domain, geographic permissions |
| Twilio receives outbound call but rejects it | SIP digest credential, caller ID authorization, trial-account restrictions |
| Phone rings but no audio | SDP public IP or inbound RTP firewall |
| Twilio prompt heard but recording not played back | outbound RTP firewall or Asterisk media routing |
| Inbound call never reaches Asterisk | origination URI, signaling firewall, number not associated with trunk |
| One-way audio only after transfer | RTP range, transfer destination, NAT, direct media setting |
| Direct call works but Google call loops | call-gate timing or announcement pattern |
| Echo test works but AI is silent | AVA provider, ARI application registration, STT/TTS/model pipeline |
| Dashboard works but SIP does not | HTTP reverse proxy is unrelated to SIP/RTP allocations |

Useful logs:

```bash
docker compose logs -f floodman-voice

docker compose exec floodman-voice \
  asterisk -C /home/container/data/asterisk/etc/asterisk.conf -rx 'pjsip show endpoints'

docker compose exec floodman-voice \
  asterisk -C /home/container/data/asterisk/etc/asterisk.conf -rx 'pjsip set logger on'
```

Disable SIP logging after diagnosis because it can expose call metadata.
