# Asterisk and FreePBX

## Embedded mode

The default deployment uses the Asterisk package inside the AIO container. `scripts/render_asterisk.py` generates:

- `asterisk.conf`
- `modules.conf`
- `pjsip.conf`
- `extensions.conf`
- `ari.conf`
- `manager.conf`
- `http.conf`
- `rtp.conf`
- logger, CDR, and voicemail configuration

All writable Asterisk paths are under `DATA_DIR/asterisk`. The process runs as the container user and does not require root after the image is built.

## External Asterisk or FreePBX mode

Set:

```dotenv
ASTERISK_MODE=external
ASTERISK_HOST=<pbx-address>
ARI_PORT=8088
ARI_USERNAME=floodman-ava
ARI_SECRET=<strong secret>
AMI_HOST=<pbx-address>
AMI_PORT=5038
AMI_USERNAME=floodman
AMI_SECRET=<strong secret>
```

The external PBX must provide:

- Asterisk 18 or newer
- ARI HTTP enabled
- An ARI user with read and write access
- AMI originate access for outbound automation
- AudioSocket or an AVA-supported external-media transport
- A dialplan route into `Stasis(asterisk-ai-voice-agent)`
- Network access from the AVA container to ARI, AMI, and the selected audio transport

## External inbound dialplan

The embedded dialplan uses two AGI scripts around the gate stream:

```asterisk
same => n,Answer()
same => n,AGI(/path/to/agi_gate_start.py)
same => n,GotoIf($["${FLOODMAN_GATE_BYPASS}"="1"]?ava)
same => n,AudioSocket(${FLOODMAN_GATE_UUID},floodman-host:9019)
same => n,AGI(/path/to/agi_gate_finish.py)
same => n(ava),Stasis(asterisk-ai-voice-agent)
```

The scripts must have access to `FLOODMAN_INTERNAL_URL` and `INTERNAL_TOKEN`.

In FreePBX, place custom dialplan in `extensions_custom.conf` and route the inbound DID to a Custom Destination. Do not edit generated FreePBX dialplan files directly.

## Human transfers

The AVA transfer tools target extensions configured for:

- live reception
- emergency on-call
- billing
- estimating

Embedded mode maps `6000` through `6003` to the phone numbers supplied in environment variables. External FreePBX can instead map those extensions to ring groups, queues, extensions, or custom destinations.

## Provider failure

When AVA cannot handle the Stasis call, the embedded dialplan routes to the configured live-reception destination. Confirm this route before moving the public number.

## Carrier testing

Test at least:

- inbound direct call
- inbound Google forwarding call
- outbound call
- caller hang-up during opening gate
- human transfer with answer
- human transfer with no answer
- DTMF and voicemail behavior
- G.711 µ-law and A-law
- NAT from cellular and landline callers
- simultaneous calls
- call after AVA restart
- call after Roomflow is deliberately unavailable
