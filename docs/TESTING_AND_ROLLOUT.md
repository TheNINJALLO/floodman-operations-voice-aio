# Testing and Rollout

The rollout deliberately separates carrier transport, Asterisk, AVA, Google classification, Roomflow, and outbound automation. A successful AI demo does not prove the SIP or compliance paths, and a successful echo test does not prove the AI pipeline.

## Automated validation

```bash
make validate
make test
```

Strict configuration preflight:

```bash
python3 scripts/preflight.py \
  --env-file .env \
  --env-file .env.twilio-provisioning \
  --require-provisioning \
  --strict
```

CI also builds the container image. A release should not be promoted when tests, configuration validation, strict preflight, or the image build fail.

## Stage 0: Offline configuration

Keep public calling disabled:

```dotenv
SIP_TRUNK_MODE=disabled
OUTBOUND_ENABLED=false
ROOMFLOW_ENABLED=false
```

Validate:

- dashboard login
- runtime secret persistence
- API docs disabled
- customer, property, invoice, and consent imports
- billing blocked before verification
- temporary verification session after approved facts
- upload-link generation and expiry
- suppression and partial revocation
- Roomflow staging mappings
- backup creation and restore into a separate instance

Exit criteria:

- zero critical diagnostic failures unrelated to intentionally disabled SIP
- database backup and integrity check pass
- no secret printed or committed

## Stage 1: Twilio configuration plan

- Fill `.env` and `.env.twilio-provisioning`.
- Run strict preflight.
- Run `twilio-config` and `twilio-plan`.
- Review any phone-number routing conflict.
- Apply and verify the trunk.
- Confirm the Twilio number is associated with the intended trunk.
- Confirm firewall ranges and geographic dialing permissions.

Exit criteria:

- `twilio-verify` reports no pending actions
- no unintended webhook or application route was replaced

## Stage 2: Carrier termination test

Test Asterisk to Twilio without AVA:

- call Twilio’s free `Play` test numbers through the trunk
- hear the prompt
- record speech
- hear the recording played back

Exit criteria:

- outbound SIP authentication succeeds
- two-way RTP succeeds
- E.164 caller and destination formatting is accepted

## Stage 3: Carrier origination test

Use Twilio Console’s trunk origination test to call the associated DID.

Validate:

- Asterisk receives the call
- DID is recovered correctly, including from the `Diversion` header
- call gate starts and exits
- audio works both directions
- AVA or human fallback answers

Exit criteria:

- no one-way audio
- no 30-second signaling timeout
- no public exposure of ARI or AMI

## Stage 4: Controlled operator echo call

Enable only one operator-owned destination:

```dotenv
AMI_ENABLED=true
TEST_CALLS_ENABLED=true
TEST_CALL_ALLOWLIST=+1XXXXXXXXXX
```

Place the test from the dashboard. The operator must hear the Asterisk prompt and their own speech returned.

Exit criteria:

- AMI authentication succeeds
- Twilio termination succeeds
- operator phone answers
- two-way RTP succeeds
- test is recorded in the event ledger

Set `TEST_CALLS_ENABLED=false` after the test.

## Stage 5: Direct inbound temporary DID

Use a temporary DID, not Floodman’s primary number.

Test from multiple cellular networks and with realistic background noise:

- immediate opening speech
- silence
- interruptions
- long address and email
- emergency water intake
- ordinary inspection request
- existing customer
- caller requests a person
- abrupt disconnect
- no-answer human transfer
- AVA provider unavailable
- Roomflow unavailable

Exit criteria:

- opening speech preserved
- silent direct caller greeted within the configured short timeout
- emergency task usable by staff
- human fallback works
- local record persists when Roomflow is down
- no private billing information before verification

## Stage 6: AVA acceptance

Confirm diagnostics show the AVA Stasis application registered.

Complete calls through the selected production STT, model, and TTS path:

- new-customer intake
- service-area check
- appointment availability
- appointment creation
- callback task
- existing-customer verification
- billing-summary boundary
- payment-link delivery
- human transfer

Measure:

- response latency
- barge-in behavior
- false interruptions
- address and phone-number accuracy
- tool execution result
- final call disposition

Exit criteria:

- no repeated greeting or self-audio loop
- tools produce expected local and Roomflow records
- provider failure reaches a safe human route

## Stage 7: Google shadow mode

Use a real Google forwarding path while staff remain the operational fallback.

Review every call against the gate result:

- forwarding announcement detected
- agent stays quiet during announcement
- live customer detected
- first customer sentence preserved
- automated business-information call restricted
- suspicious Google request routed to security
- uncertain call fails open to ordinary service

Do not tune only from synthetic transcripts. Carrier audio and announcement timing are the real acceptance environment.

Exit criteria:

- acceptable false-positive and false-negative rate from reviewed real calls
- no announcement loops
- no customer held silently beyond the gate timeout

## Stage 8: After-hours and overflow

Route only after-hours or overflow calls to the system.

- review every transcript and disposition
- confirm emergency notifications reach a person
- confirm callback records contain usable contact and property details
- confirm failed AI calls reach human fallback or safe voicemail
- monitor disk, CPU, memory, RTP ports, and response latency

Exit criteria:

- operational team can trust the generated lead and callback records
- no unresolved critical readiness alerts

## Stage 9: Primary receptionist

Move the primary DID only after direct, Google, emergency, transfer, provider-failure, and Roomflow-outage tests pass.

Keep a one-step rollback to the existing human ring group. Do not enable outbound campaigns during the cutover.

Monitor:

- SIP failures
- one-way audio
- gate timeouts
- AVA registration
- emergency escalations
- human transfer outcomes
- Roomflow outbox growth
- customer complaints

## Stage 10: Requested callbacks

Enable AMI and outbound worker only for explicit customer-requested callbacks.

Validate:

- caller ID
- disclosure
- no-answer
- busy
- voicemail
- opt-out
- human transfer
- duplicate protection
- uncertain completion moves to manual review rather than an automatic second call

## Stage 11: Billing and marketing

Enable only after legal and operational review.

- audit exact consent evidence
- connect DNC screening
- connect reassigned-number screening
- verify customer-local calling windows
- verify conservative attempt limits
- pace calls to the Twilio trunk CPS limit
- run internal seed-list campaigns
- review every call before increasing volume

## Acceptance matrix

| Scenario | Required result |
|---|---|
| Twilio Play termination | prompt, record, and playback succeed |
| Twilio origination test | Asterisk answers with two-way audio |
| Controlled operator echo | operator hears returned speech |
| Direct customer talks immediately | opening request preserved |
| Direct customer is silent | normal greeting after short timeout |
| Google forwarding announcement | agent remains quiet until customer |
| Google automated business call | approved public answers only |
| Fake Google credential request | security event and no sensitive disclosure |
| Active flooding | emergency lead and immediate callback task |
| Roomflow offline | local record and durable outbox |
| Calendar unavailable | provisional availability and safe wording |
| Billing before verification | no private invoice details |
| Billing after verification | scoped summary and secure-link option |
| Customer says stop | immediate category suppression |
| Marketing without written consent | blocked before AMI |
| Marketing without current DNC check | blocked before AMI |
| AVA failure | human fallback route |
| Gate STT failure | ordinary customer-service route |
| Completion webhook missing | manual review, not duplicate redial |

## Evidence to retain

For every acceptance call, record:

- test date and operator
- software Git commit and image digest
- Twilio trunk and edge
- source and destination DID
- codec and secure-media mode
- Asterisk unique ID
- Floodman call ID
- gate classification and confidence
- opening transcript
- AVA agent and provider
- tool execution results
- transfer outcome
- Roomflow result or outbox ID
- operator review result
- corrective change and retest result
