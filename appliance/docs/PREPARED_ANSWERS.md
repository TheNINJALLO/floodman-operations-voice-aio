# Common caller questions

The approved document `knowledge/02-common-service-questions.md` contains 36
short answers, caller phrasings and service-specific follow-ups. The running
copy lives under `DATA_DIR/knowledge` and can be edited in the existing
Knowledge area. It is an answer bank; it does not impose a sequence of questions
or perform any business action. No Outreach request or model inference is
needed for a recognized question.

Each section is YAML beneath a Markdown `##` heading. `questions` lists complete
caller phrasings. `followups` lists wording that needs the recent service topic,
such as "How does it work?". `answer` is the spoken reply. `source` identifies
an approved fact document, and `source_sha256` binds the reply to its reviewed
body. The digest uses SHA-256 of UTF-8 body text with whitespace collapsed.
Source documents must remain approved and unchanged. Any substantive edit
disables affected prepared replies until the replies and digest are reviewed.
Do not update a digest merely to silence a stale-source warning.

The matcher removes social introductions and supports a few harmless spelling
variants. It preserves negation and price wording. It does not use fuzzy token
similarity to turn a diagnosis, price or guarantee question into a different
answer. Unknown wording continues through approved knowledge and the existing
bounded local model. Prepared catalog bodies are excluded from ordinary fact
retrieval so their configuration never becomes model evidence.

The existing Knowledge approval switch disables a whole bank. Source withdrawal
also disables its replies. Invalid entries fail open to the existing answer
path; they cannot block the phone system. New source files, drafts and learned
observations do not silently add approved replies. Review is still required.

For an existing installation, back up data and add only the new catalog to
`DATA_DIR/knowledge` before starting the new image. Verify its source hashes
against the installed approved documents. Preserve all existing knowledge
edits; do not replace the knowledge directory. Fresh installations receive the
catalog in the normal knowledge template. Deleting or unapproving the runtime
catalog is respected across restarts; the image does not reseed it into an
existing knowledge directory.

Deterministic emergency routing and protected decisions run before answers.
No consent, suppression, DNC, quiet-hour, verification, routing or billing
decision is made by a prepared reply. Per-call diagnostics record the matched
FAQ ID without copying caller text. Tests cover every caller phrasing, topic
switches, later intake stages, ambiguous wording, source edits, approval and
the existing emergency route.
