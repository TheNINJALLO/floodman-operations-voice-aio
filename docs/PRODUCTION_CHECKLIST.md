# Production Readiness Checklist

Use this checklist before moving Floodman’s primary number or enabling outbound campaigns.

## Release and host

- [ ] Deploy a tagged release, not an uncommitted working tree.
- [ ] Record the Git commit and image digest.
- [ ] Use a supported Linux host with current security updates.
- [ ] Confirm adequate CPU, memory, disk, and bandwidth for peak concurrent calls.
- [ ] Keep at least 100 kbps of bidirectional bandwidth per G.711 call, plus operational headroom.
- [ ] Confirm the host has a static, globally routable public IP.
- [ ] Confirm SIP and RTP do not depend on an HTTP reverse proxy or carrier-grade NAT.
- [ ] Configure time synchronization and `America/Detroit` as the application business timezone.

## DNS and HTTPS

- [ ] Point the voice dashboard DNS name to the host.
- [ ] Use Caddy or an equivalent reverse proxy with automatic certificate renewal.
- [ ] Keep the application dashboard bound to loopback or a private network.
- [ ] Block `/internal/*` at the reverse proxy.
- [ ] Restrict `TRUSTED_HOSTS` to actual management hostnames.
- [ ] Restrict `TRUSTED_PROXY_IPS` to actual proxy sources.
- [ ] Keep API documentation disabled in production.
- [ ] Confirm HSTS and browser security headers are present.

## Firewall

- [ ] Permit HTTPS only where required.
- [ ] Permit Twilio signaling ranges to the active SIP port.
- [ ] Permit Twilio’s current global media range to the exact RTP UDP range.
- [ ] Deny public access to ARI, AMI, AudioSocket, local AI, databases, and supervisor files.
- [ ] Confirm the host firewall and the provider firewall match.
- [ ] Confirm the Docker or Pterodactyl port mappings use the same SIP and RTP port numbers inside and outside the container.
- [ ] Review Twilio’s current network-range documentation before activation.

## Twilio

- [ ] Use an owned voice-capable Twilio number.
- [ ] Use E.164 formatting with a leading `+` everywhere.
- [ ] Use a unique termination domain.
- [ ] Use a restricted API key for provisioning.
- [ ] Store Twilio REST credentials outside the runtime container.
- [ ] Use a strong SIP username and password.
- [ ] Keep `TWILIO_ALLOW_PHONE_ROUTING_CHANGE=false` except during an intentional reviewed change.
- [ ] Run `show-config`, `plan`, `apply`, and `verify`.
- [ ] Confirm the number is attached to the intended trunk.
- [ ] Confirm geographic dialing permissions cover only needed destinations.
- [ ] Confirm the caller ID is owned or verified.
- [ ] Confirm trunk transfer mode matches Floodman’s policy.
- [ ] Confirm default termination CPS is sufficient and pace campaigns to the provisioned limit.

## Asterisk and media

- [ ] Strict preflight passes with zero blocking errors.
- [ ] Asterisk loads the generated PJSIP endpoint.
- [ ] ARI and AMI bind only to loopback.
- [ ] Twilio’s signaling CIDRs are the only inbound endpoint matches.
- [ ] `direct_media=no` remains set.
- [ ] `TWILIO_RTP_SYMMETRIC=false` remains set unless a documented NAT diagnosis requires otherwise.
- [ ] The advertised SIP and SDP public address is correct.
- [ ] The RTP range contains enough ports for expected calls and transfers.
- [ ] Test DTMF if any human destination or payment IVR needs it.
- [ ] Disable verbose SIP logging after testing.

## Carrier acceptance

- [ ] Twilio `Play` termination test succeeds with record and playback.
- [ ] Twilio Console origination test reaches Asterisk with two-way audio.
- [ ] Controlled operator echo call succeeds.
- [ ] Direct inbound cellular calls succeed from at least two carriers.
- [ ] Caller speaks immediately and opening speech is preserved.
- [ ] Silent caller receives the ordinary greeting.
- [ ] Barge-in works without repeated self-interruption.
- [ ] Reception transfer works.
- [ ] Emergency transfer works.
- [ ] Billing transfer works.
- [ ] Estimating transfer works.
- [ ] No-answer, busy, rejected, and disconnected calls produce safe outcomes.

## AVA and Call Gate

- [ ] AVA Stasis application is registered in ARI.
- [ ] Selected STT, model, and TTS pipeline is configured and stable.
- [ ] Median conversational latency is acceptable on real cellular calls.
- [ ] Gate STT failure routes to ordinary customer service.
- [ ] AVA provider failure routes to a human fallback.
- [ ] Direct customer intake creates a usable record.
- [ ] Emergency intake creates the correct priority task.
- [ ] Existing-customer lookup does not expose private information before verification.
- [ ] Billing data remains scoped to the verified customer.
- [ ] Payment URLs are delivered through the approved adapter and never spoken back as raw secrets.

## Google forwarding

- [ ] Test through a real Google forwarding path.
- [ ] Agent remains quiet during the announcement.
- [ ] Customer is greeted after the announcement.
- [ ] Customer’s first sentence is preserved.
- [ ] Low-confidence calls fail open to normal service.
- [ ] Automated Google business calls receive approved public information only.
- [ ] Requests for passwords, codes, payment, remote access, or listing fees trigger the security route.
- [ ] Review false positives and false negatives from actual recordings and transcripts.

## Roomflow

- [ ] Use staging credentials first.
- [ ] Map every required endpoint explicitly.
- [ ] Verify authentication, company ID, TLS, and idempotency headers.
- [ ] Create a new customer, property, lead, emergency case, appointment, callback, and call outcome.
- [ ] Confirm duplicate calls do not create duplicate records.
- [ ] Stop Roomflow and confirm local records plus outbox entries.
- [ ] Restore Roomflow and confirm outbox replay.
- [ ] Keep `ROOMFLOW_ENABLED=false` until staging results match expectations.

## Security and privacy

- [ ] `.env`, provisioning credentials, certificates, recordings, uploads, and customer exports are not committed.
- [ ] `runtime.env` is mode `0600`.
- [ ] Bootstrap secrets are not printed to shared logs.
- [ ] Admin token is stored on a protected operator device and used only over HTTPS.
- [ ] Internal token is not used in browser code.
- [ ] Upload expiry, file-size, type, and storage controls are tested.
- [ ] Add malware scanning before opening uploads to large public campaigns.
- [ ] Establish recording disclosure and retention rules.
- [ ] Do not record or transcribe full card or bank credentials.
- [ ] Establish a documented incident and credential-rotation process.

## Call recording

- [ ] `CALL_RECORDING_ENABLED=false` until legal/compliance sign-off on disclosure language.
- [ ] Recording disclosure script reviewed and approved by legal counsel.
- [ ] `CALL_RECORDING_DISCLOSURE_ENABLED=true` confirmed before enabling recording.
- [ ] `CALL_RECORDING_RETENTION_DAYS` set to match your data-retention policy.
- [ ] `CALL_RECORDING_STORAGE_DIR` is inside `/home/container/data` and excluded from `.gitignore`.
- [ ] Dashboard recording access tested for each role (owner, manager, staff).
- [ ] Unauthorized access to `/api/v1/recordings/*` returns 401.
- [ ] Streaming endpoint tested with HTTP Range header (partial content 206).
- [ ] Path traversal to recordings directory rejected with 403.
- [ ] Legal hold workflow tested: held recordings survive retention cleanup.
- [ ] Retention cleanup dry-run tested before enabling scheduled cleanup.
- [ ] Payment segment pause confirmed: no card/CVV/bank data in recording files.
- [ ] SHA-256 digest verified for at least one finalized recording.
- [ ] Recording metadata persists after container restart.
- [ ] Recording failure during active call does not disconnect the caller.
- [ ] Backups do not include recordings by default (PII — contains customer voice data).
- [ ] `--include-media` backup flag documented and restricted to authorized personnel.


- [ ] Automated daily backup runs successfully.
- [ ] Backup is copied off the voice host.
- [ ] Runtime and Twilio provisioning secrets are stored separately in encrypted secret storage.
- [ ] Backup manifest and checksums verify.
- [ ] Restore has been tested into a separate stopped instance.
- [ ] Recovery steps include ownership UID 988 and database integrity checks.
- [ ] Record target recovery point and recovery time objectives.

## Monitoring

- [ ] Monitor `/livez` and `/readyz` from an external probe.
- [ ] Alert on `readyz` failure, repeated Asterisk restarts, AVA registration loss, low disk, and outbox growth.
- [ ] Alert on emergency callback failures.
- [ ] Alert on outbound jobs entering manual review.
- [ ] Review Twilio Voice Insights and SIP error trends.
- [ ] Rotate and cap container logs.
- [ ] Track call-gate classifications and human review outcomes.

## Outbound compliance gate

Keep `OUTBOUND_ENABLED=false` until all applicable items are complete:

- [ ] Counsel has reviewed transactional and marketing call policy.
- [ ] Consent evidence stores exact text, version, source, number, and timestamp.
- [ ] Category-specific revocation works.
- [ ] Immediate verbal opt-out works.
- [ ] Internal suppression list works.
- [ ] DNC screening process is connected and current.
- [ ] Reassigned-number screening process is connected and current.
- [ ] Customer-local calling windows are correct.
- [ ] Frequency caps are conservative.
- [ ] Voicemail scripts reveal no private billing or property details.
- [ ] Billing agent verifies identity before account disclosure.
- [ ] Marketing campaigns contain only written-consent-qualified numbers.
- [ ] Campaign pacing stays within Twilio’s provisioned CPS.
- [ ] Seed-list calls have been reviewed end to end.

## Primary-number cutover

- [ ] Use a temporary DID for acceptance first.
- [ ] Preserve a one-step rollback to the existing human route.
- [ ] Schedule cutover with an operator monitoring live logs and Twilio call logs.
- [ ] Complete direct, emergency, transfer, provider-failure, and Google tests after cutover.
- [ ] Review every call during the initial production window.
- [ ] Do not enable outbound campaigns during the inbound cutover.
