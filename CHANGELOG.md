# Changelog

## 1.1.1 - 2026-08-11

- Fixed Pterodactyl egg import 500 caused by a non-email author value.
- Normalized boolean defaults to `0` and `1` for server-variable validation.
- Switched to the maintained Pterodactyl Debian installer image.
- Removed the duplicate image selector.


## 1.1.0 - 2026-08-11

- Added a first-class Twilio Elastic SIP Trunking runtime profile for Asterisk PJSIP
- Added localized Twilio termination URI support, digest authentication, inbound signaling CIDR identification, E.164 DID handling, and `Diversion` header extraction
- Added initial UDP/RTP and optional TLS plus SDES-SRTP deployment profiles
- Disabled Twilio symmetric RTP by default and exposed an explicit diagnosed-NAT override
- Added one-time Twilio provisioning with secret-free `show-config`, read-only `plan`, guarded `apply`, and drift-aware `verify`
- Split Twilio REST API credentials from long-running SIP runtime credentials
- Added a guard that refuses to silently replace an existing number webhook, application, or trunk route
- Added controlled outbound operator echo calls protected by a feature flag and E.164 allowlist
- Added public minimal liveness/readiness probes and authenticated detailed Asterisk, ARI, AMI, AVA, Twilio DNS, and configuration diagnostics
- Added strict static production preflight for public addressing, HTTPS, trusted hosts, trusted proxies, origination URI alignment, RTP ranges, E.164 numbers, and TLS files
- Added runtime secret persistence with mode `0600` and no default secret printing
- Disabled API documentation by default and added browser security headers
- Added optional Caddy HTTPS deployment that blocks public `/internal/*` access
- Hardened Docker deployment with non-root execution, dropped capabilities, no-new-privileges, log rotation, tmpfs, PID limits, and file-descriptor limits
- Fixed dynamic SIP, TLS, and RTP Docker port mappings
- Added safe non-executing environment-file loading for provisioning and preflight scripts
- Added live-safe SQLite backup archives with integrity checks, manifests, SHA-256 verification, restrictive permissions, and retention pruning
- Added production runbooks for Twilio, Pterodactyl, backups, security, acceptance testing, and cutover

## 1.0.0 - 2026-08-11

- Initial Floodman Operations Voice AIO release
- Embedded Asterisk and AVA deployment
- Fail-closed AVA HTTP body-template safety patch for quoted and multiline customer values
- Google-aware AudioSocket call gate with a short direct-caller no-speech fallback
- Seven purpose-specific Floodman agents
- Local CRM and Roomflow retry outbox
- Server-validated scheduling, cross-customer billing isolation, masked lookup, secure payment-link delivery, uploads, callbacks, and emergency intake
- Outbound campaign and evidence-backed compliance engine with partial revocation and stale-call manual review
- Batch campaign audience enrollment with consent reuse and duplicate protection
- Responsive web dashboard
- Docker Compose, Pterodactyl egg, GitHub Actions, and automated tests
