# Quick Start

1. Push this directory to `TheNINJALLO/floodman-voice-appliance`.
2. Wait for the GitHub container workflow to publish the `latest` image.
3. Install NVIDIA Container Toolkit and expose the RTX A1000 to Pterodactyl containers.
4. Import `pterodactyl/egg-floodman-voice-appliance.json`.
5. Assign TCP 8003, SIP 5060, and UDP 10000-10100.
6. Configure SIP, transfer numbers, and Twilio SMS credentials.
7. Start the container and wait until `/ready` is healthy.
8. Retrieve `ADMIN_TOKEN` from `data/runtime.env` if it was generated, use **Recovery access** on `/login`, and create the first named administrator.
9. Sign in with the new username and password. Open **Notifications** on each user's HTTPS device and enable browser alerts.
10. Test `/simulator` before routing the live DID.
11. Run one full call, one hangup, one unsupported request, and one human transfer.
