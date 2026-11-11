# Local call learning

Learning is an optional background service with its own private database under
`DATA_DIR/learning`. Calls do not query Outreach and continue when imports stop
or the provider is unavailable. The existing local voice model and speech stack
are preserved. This feature analyzes calls and prepares knowledge drafts; it
does not fine-tune model weights.

The operator provisions `DATA_DIR/learning/connection.json` with `environment`,
`account_id`, and an account-only `api_key`. Never commit this file or return it
to browsers. The learning directory is private and the file is mode 600.
Controls are persisted separately; learning starts disabled until enabled by
an authorized manager/operator. A changed account/key triggers fresh identity
and project verification. All provider requests are GETs to the fixed official
API origin, with bounded responses, pagination and sanitized retries.

The first scan covers every accessible project, including inactive projects,
and all paginated contacts/conversations. It checkpoints after each step.
Only ended conversations are analyzed. Provider system/tool messages are
excluded. Names matching contact fields, common contact patterns and addresses
are redacted before storage. Redaction is imperfect; staff must remove any
remaining personal details from published answers. Source text expires after
30 days, including unpublished analysis text. Repeat polling of unchanged
source text does not extend retention.

History is collected before inference begins. The inference queue prioritizes
historical records, including delayed retries, before newly discovered calls.
Scans repeat 15 minutes after completion and take additional worker time.
Importing and model analysis run in separate tasks. Long model reviews do not
delay provider page reads or collection of Avery's own calls. Live calls still
pause background work. A source removed during a scan causes a fresh verified
scan rather than permanently retrying a stale contact or conversation ID.
The manual check control also requests fresh provider pages. Concurrent source
changes invalidate old model output before it is recorded as a finished review.
Changed conversations invalidate their previous analysis; stable IDs prevent
duplicate examples. Active calls discovered during scanning are revisited on
later scans. Empty, failed, expired and attention states remain visible.

Avery's own ended calls also enter the learning queue. Simulations/deployment
checks are excluded, and concurrent calls ending out of order are retained.
Local feedback categories and measured audio diagnostics supplement dialogue;
provider text alone cannot establish audio latency or interruption quality.
Staff briefings take precedence over learning. Analysis waits during live
calls and cancels/requeues if one starts. Storage/network work runs outside
the voice event loop. Optional learning initialization failures leave calls
available. Model failures are retried up to three times, then need attention.

The local model uses the configured local briefing backend and produces a
bounded structured review with exact source quotes. Quotes are validated.
No provider text is allowed to set routing, consent, suppression, verification,
billing, recipients or contact permissions. Approximately one fifth of stable
conversation IDs are reserved as holdout data and cannot be published.

Managers open `/learning` to review handling, evidence and suggested answers.
Publishing requires an edited question/answer and explicit confirmation that
facts were checked and personal data removed. The source digest is rechecked
under a local write lock. Approved answers become normal approved local
Knowledge documents; existing company answers can then retrieve them. Rejected
or unreviewed drafts never affect Avery. Published knowledge can be edited or
removed through Knowledge, independently of the provider connection.

Stopping Outreach imports is local: it neither cancels the vendor subscription
nor changes campaigns or phone routing. Learning from Avery's calls continues.
Retirement requires separately comparing call quality, transfer behavior,
message/briefing delivery and response timing on representative real calls.
Dataset holdouts are reserved for future evaluations; a completed import or
model review does not itself establish performance parity or cutover readiness.
