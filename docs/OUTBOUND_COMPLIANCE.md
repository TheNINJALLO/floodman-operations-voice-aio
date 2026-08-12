# Outbound Calls and Compliance

## Important boundary

The compliance engine enforces the configured technical policy. It does not replace legal advice. Federal, state, industry, contractual, carrier, and consent requirements can differ by call purpose and customer location. Have qualified counsel approve the final policy before automated outbound calls are enabled.

## Call purposes

### Explicit callback

- requested callback
- missed-call callback

A recent customer request can satisfy the configured callback eligibility rule. The request timestamp is stored and checked again before dialing.

### Transactional

- billing reminder
- payment follow-up

These require a non-revoked transactional artificial-voice consent record in the included default policy. The record must retain its source, exact text version, exact consent text, and timestamp.

### Marketing

- estimate follow-up
- canceled-inspection recovery
- win-back
- maintenance outreach

These require a non-revoked written automated-marketing consent record with retained evidence, plus current Do Not Call and reassigned-number attestations under the default policy.

## Consent records

A consent record stores:

- exact phone number
- customer ID
- transactional voice permission
- written marketing voice permission
- SMS and email permission
- source
- exact consent-text version
- exact consent text
- timestamp
- revocation timestamp
- raw evidence metadata

Do not reduce consent evidence to a Boolean without retaining the exact disclosure and source. The default engine fails closed when consent evidence is missing or dated after the scheduled call. Category-specific revocation is recorded separately, so a marketing opt-out does not silently erase a still-valid transactional preference unless the customer revokes all contact categories.

## Opt-outs

The voice tool accepts category-specific suppression:

- `all`
- `marketing`
- `transactional`
- `callbacks`
- an exact outbound purpose

An opt-out writes the suppression immediately, records the event, and attempts Roomflow synchronization. Every outbound job is rechecked before dialing, so queued jobs are blocked after the suppression appears.

## External attestations

Marketing job payloads must include current screening evidence:

```json
{
  "dnc_status": "clear",
  "dnc_checked_at": "2026-08-11T12:00:00Z",
  "reassignment_status": "not_reassigned",
  "reassignment_checked_at": "2026-08-11T12:00:00Z"
}
```

The allowed statuses and maximum ages are configurable. Integrate authoritative screening services rather than self-attesting without a real check.

## Calling windows

Windows are evaluated in the customer’s timezone. The included defaults are deliberately conservative and disable Sunday marketing calls. Blackout dates can be added in ISO date form.

Emergency callbacks requested by the customer can be configured to bypass ordinary windows. This exception does not apply to marketing or billing campaigns.

## Loading campaign audiences

The admin panel and `POST /api/v1/campaigns/{campaign_id}/enqueue` endpoint accept up to 1,000 audience entries per request. Enrollment:

- reuses the current consent record stored for each normalized phone number
- prevents a second open job for the same phone and campaign
- staggers scheduled times by the configured spacing interval
- copies DNC and reassignment evidence into each job payload
- returns an eligibility preview for each new job
- performs the complete eligibility check again immediately before Asterisk dials

A campaign can be loaded while it is in `draft` or `paused` state. Its jobs remain dormant until the campaign is changed to `active`.

## Frequency limits

Each purpose has:

- rolling-window days
- maximum attempts
- cooldown after a live conversation

Attempts are counted from durable outbound-job records. Live-contact events are recorded only for connected conversations and defined outcomes, not for every AMI originate request. If Asterisk accepted a call but its completion webhook is later missing, the stale job is marked for manual review rather than automatically redialed. This prevents an uncertain completion record from becoming a duplicate customer call.

## Billing privacy

The billing agent:

- identifies itself as Floodman’s automated assistant
- verifies the customer before revealing private account information
- does not reveal balances in voicemail
- never asks for a complete card number, CVV, bank credentials, Social Security number, password, or one-time code
- verifies that the selected invoice belongs to the verified customer
- sends a secure payment link or transfers to a compliant payment path
- never returns the payment URL to the language model

## Recommended activation order

1. Requested callbacks only
2. Missed-call callbacks
3. Friendly billing reminders
4. Estimate follow-up for records with verified written consent
5. Canceled-inspection recovery
6. Maintenance and win-back only after suppression, DNC, reassignment, and audit reporting are proven

## Official reference starting points

- FCC artificial or prerecorded voice rules: `https://www.ecfr.gov/current/title-47/chapter-I/subchapter-B/part-64/subpart-L/section-64.1200`
- FTC Telemarketing Sales Rule guidance: `https://www.ftc.gov/business-guidance/resources/complying-telemarketing-sales-rule`
- National Do Not Call Registry business guidance: `https://telemarketing.donotcall.gov/`
- Reassigned Numbers Database: `https://www.reassigned.us/`

Confirm current requirements at deployment time.
