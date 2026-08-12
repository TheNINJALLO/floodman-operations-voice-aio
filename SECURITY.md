# Security Policy

## Supported release

Security fixes are applied to the latest tagged release. Record the deployed Git commit and container image digest so an incident can be tied to an exact build.

## Secret classes

### Runtime secrets

The long-running service may contain:

- Twilio SIP username and password
- Roomflow token and company ID
- AVA speech, model, and voice provider credentials
- ARI and AMI secrets
- admin, internal, and upload-signing tokens
- email or SMS provider credentials

The first container launch creates missing application, ARI, and AMI secrets in:

```text
DATA_DIR/runtime.env
```

The file is created with mode `0600`. Bootstrap secrets are not printed unless `PRINT_BOOTSTRAP_SECRETS=true` is deliberately enabled.

### One-time Twilio provisioning secrets

The following do not belong in the running service:

- Twilio Account Auth Token
- Twilio API key secret
- `.env.twilio-provisioning`

Use them only from a trusted provisioning workstation or short-lived administrative job. Prefer a restricted Twilio API key. Store the provisioning file in encrypted secret storage and never mount it into the voice container.

### Never commit

Never commit:

- `.env` or `.env.*` files other than the provided examples
- SIP, Roomflow, Twilio, Google, speech, model, email, or SMS credentials
- TLS private keys
- customer exports
- recordings or transcripts
- invoices
- uploaded photos or documents
- operational SQLite databases
- generated `runtime.env`

The repository ignores these paths, but source-control hygiene remains an operator responsibility.

## Network boundary

Publicly expose only:

- dashboard and upload portal through HTTPS
- the carrier-required SIP signaling port
- the exact Asterisk RTP UDP range

Keep private:

- ARI
- AMI
- call-gate AudioSocket
- AVA AudioSocket
- local AI WebSocket
- supervisor interfaces
- SQLite files
- application configuration containing provider keys

ARI and AMI bind to `127.0.0.1` in embedded mode. The Caddy configuration blocks `/internal/*` before proxying to the application.

Twilio inbound signaling is identified by the configured Twilio signaling CIDRs. Review Twilio’s current networking documentation before production activation. Twilio’s media range and the local RTP range must be allowed by both the host firewall and any cloud firewall.

`TWILIO_RTP_SYMMETRIC=false` is the preferred policy when Asterisk advertises the correct public address. Symmetric RTP source latching should be enabled only as a documented NAT workaround.

## HTTPS and proxy trust

- Use HTTPS for the dashboard and upload portal.
- Restrict `TRUSTED_HOSTS` to actual management hostnames.
- Restrict `TRUSTED_PROXY_IPS` to actual reverse-proxy sources.
- Never use `*` for trusted hosts or proxy sources in production.
- Keep `ENABLE_API_DOCS=false` on a public endpoint.
- Keep the dashboard bound to loopback when Caddy runs on the same host.
- Verify HSTS, CSP, frame denial, content-type protection, and no-store headers.

The admin UI stores the operator-entered token in browser local storage. Use a dedicated protected device, HTTPS, and a private management path. Rotate the admin token after suspected browser or device compromise.

## Twilio number routing guard

Associating a Twilio number with an Elastic SIP Trunk can replace existing webhook or application routing. `scripts/twilio_bootstrap.py` reads the number first and refuses to change conflicting routing unless:

```dotenv
TWILIO_ALLOW_PHONE_ROUTING_CHANGE=true
```

Use that override only after reviewing the current number resource. Return it to `false` immediately afterward.

The provisioning script never prints the Twilio API secret or SIP password. The non-secret resource state file is written mode `0600`.

## Test calls

Controlled outbound echo calls are disabled by default. They require all of:

```dotenv
AMI_ENABLED=true
TEST_CALLS_ENABLED=true
TEST_CALL_ALLOWLIST=+1XXXXXXXXXX
```

The destination must be present in the E.164 allowlist. Disable the feature when testing is complete.

## Customer and billing isolation

Unverified customer lookup returns only a record identifier, masked name, phone suffix, property count, and source. It does not return addresses, email addresses, invoices, callbacks, or job history.

Protected request-envelope data is authoritative. Model-generated body values cannot switch to another customer after server-side verification.

Every invoice lookup and payment-link action verifies invoice ownership against the verified customer. The payment URL is consumed by the delivery adapter and is not returned to the conversational model.

Do not route payment-card or bank-account audio through the general AI pipeline. Use a secure payment link, a payment provider’s compliant IVR, or a trained employee. Disable recording before any path that could capture cardholder data.

## Outbound calling

The language model does not determine call eligibility. The deterministic compliance engine checks consent, category revocation, suppression, quiet hours, frequency limits, DNC attestations, reassignment attestations, and campaign purpose before AMI originates a call.

Keep `OUTBOUND_ENABLED=false` until Floodman’s policy and consent language have been reviewed. Immediate verbal opt-out must suppress future calls without model discretion.

## Uploads

Upload links are HMAC-signed, expire, limit file size, restrict allowed types, sanitize original names, generate storage names, verify basic file signatures, and store files with restrictive permissions.

Before accepting public uploads at scale, add malware scanning, quarantine, retention, and deletion workflows. Treat uploads as customer PII.

## Backups

The built-in backup excludes media and runtime secrets by default. Backups containing recordings, uploads, or secrets must be encrypted, access-controlled, copied off-host, and subject to a retention policy.

Store `.env`, `.env.twilio-provisioning`, provider credentials, and TLS private keys separately from routine database backups. Test restoration into an isolated instance.

## Logging and incident response

- Container logs are size-capped in Docker Compose.
- Disable SIP packet logging after troubleshooting.
- Do not paste live credentials, recordings, transcripts, or customer records into public issues.
- Rotate Twilio API keys, SIP credentials, Roomflow tokens, provider keys, and application tokens after suspected exposure.
- Preserve call IDs, Asterisk unique IDs, timestamps, image digest, and relevant sanitized logs during an incident.

## Reporting a vulnerability

Report privately to the repository owner. Include the affected version, reproduction steps, impact, and proposed mitigation when available. Do not include live customer data or credentials in a public report.
