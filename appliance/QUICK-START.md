# Quick Start

1. Build from the `appliance/` directory with `Dockerfile.cpu` for the standalone CPU deployment.
2. Deploy the new `cpu-deckard-<commit>` image by digest. The historical `gpu-appliance` tag belongs to the separate unified build.
3. Set `PRIMARY_MODEL_PROFILE=deckard`, `LOCAL_AI_DEVICE=cpu`, `LOCAL_AI_ONLY=true`, and `VOICE_ENGINE=tel_agent`. Deckard is the primary live model. NVIDIA hardware is optional; CUDA operation requires explicit `LOCAL_AI_DEVICE=cuda` plus the NVIDIA Container Toolkit and GPU access.
4. Import `pterodactyl/egg-floodman-voice-appliance.json`.
5. Assign TCP 8002, SIP 5060, and UDP 10000-10100.
6. Configure SIP, transfer numbers, and Twilio SMS credentials.
7. Start the container and wait until `/ready` is healthy. First boot downloads and verifies the pinned 24.3 GB Deckard GGUF unless `DECKARD_MODEL_PATH` supplies an existing verified copy. Old generic model fields are ignored in the Deckard profile.
8. Retrieve `ADMIN_TOKEN` from `data/runtime.env` if it was generated, use **Recovery access** on `/login`, and create the first named administrator.
9. Sign in with the new username and password. Add each user's email address and alert permissions, then open **Email delivery** to configure and test SMTP.
10. Open **Notifications** on each user's HTTPS device and enable browser alerts.
11. Open **Voice**, preview the desired installed voice, and save its speaking speed.
12. Test `/simulator`, then test the audio path over the actual carrier before routing the live DID.
13. Run one full call, one hangup, one unsupported request, one emergency, and one human transfer. Test short answers, pauses, spelling, and interruptions, measure response latency, and verify selected members receive the briefings.
