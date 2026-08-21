# GPU Appliance Track in the Current AIO Repository

This repository contains two isolated deployment tracks:

- The existing root application and `latest` image remain the deployed AIO system.
- `appliance/` is the clean, self-hosted NVIDIA RTX A1000 replacement.

The appliance is published as:

```text
ghcr.io/theninjallo/floodman-operations-voice-aio:gpu-appliance
```

This keeps the production `latest` tag untouched while the local-AI call path is tested.

## Deployment order

1. Run **Install GPU Appliance Into Current Floodman AIO**. The installer tests and publishes the first image directly.
2. Import `appliance/pterodactyl/egg-floodman-voice-appliance.json` into Pterodactyl.
3. Create a new test server using the `gpu-appliance` image tag.
4. Give the container access to the RTX A1000 through the NVIDIA container runtime.
5. Configure SIP/Twilio, transfer numbers, and team SMS recipients.
6. Wait for `/ready` to return HTTP 200.
7. Test the browser simulator, then a separate test DID.
8. Move the production DID only after complete, partial-hangup, unsupported-service,
   emergency, and transfer tests pass.

## What stays external

Only the SIP/SMS carrier. The appliance does not accept Deepgram, Groq,
ElevenLabs, or OpenAI API keys.

## Promotion

After acceptance testing, the appliance can replace the root AIO build in a
later commit. Until then, both implementations remain available from the same
GitHub repository without risking the live image.


## Future image builds

The reusable workflow template is stored at:

```text
appliance/ci/ci-gpu-appliance.yml
```

It is deliberately not written into the repository's root
`.github/workflows/` directory by the installer. GitHub rejects root workflow
changes pushed by a GitHub App token without the separate Workflows
permission. Copy the template manually with an appropriately authorized
credential when recurring automatic builds are desired.
