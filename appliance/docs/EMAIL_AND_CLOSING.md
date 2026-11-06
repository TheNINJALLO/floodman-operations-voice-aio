# Email readback and call closing

Email confirmations spell the local part and read domains naturally at normal
speed (`EMAIL_READBACK_SPEED=1.0`). Digits and actual punctuation are explicit:
`alex.smith02@gmail.com` becomes “a, l, e, x, dot, s, m, i, t, h, zero, two at gmail dot com.”
Nothing changes the stored address or confirms it without the caller's answer.
“Spell it out” during email confirmation spells the local part while keeping the
domain readable. Repeated unclear confirmations still use the existing bounded
retry and phone-verification fallback.

The final message question is asked once. Extra details receive a short
acknowledgment, with the floor left open. After a completed extra detail, silence
ends with a warm farewell instead of another question. Holds and unfinished
thoughts still wait; the phone driver defers silence handling while speech or
recognition is active. Complete answers such as “that should be it,”
“I think that's all,” and “I'm all set” close the intake before contact extraction.
Matching is limited to an entire closing answer. “That should be it, but please
use the side door” remains a message, and active flooding still takes priority.
The farewell stays interruptible for corrections and completion notifications
remain deferred until playback finishes on a phone call.

`scripts/smoke_email_closing.py` compares actual Pocket audio against the previous
whole-address letter-by-letter readback, checks intelligibility with the local recognizer, and
passes a synthesized closing answer through the deterministic intake. Results
and synthetic WAV files stay under `DATA_DIR/email-closing-acceptance`. This does
not measure carrier playback or guarantee subjective voice quality on a call.
