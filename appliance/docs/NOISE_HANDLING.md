# Noise-tolerant call interruption

The streaming profile requires sustained, confident speech above an energy floor
before stopping playback. Silero probability no longer bypasses the configured
volume threshold. The profile defaults are 320 ms of speech, 500 PCM RMS, a 0.75
speech probability, and at most 60 ms between voiced fragments.

A per-call playback reference suppresses highly correlated speakerphone echo
before interruption detection and recognition. Unrelated overlapping caller
audio is retained. This is conservative reference subtraction, not full acoustic
echo cancellation or speaker identification. Radio speech and unusual acoustic
paths still require real-line evaluation.

If playback was interrupted but no caller response arrives, the local phone
driver can repeat the interrupted prompt after its short recovery check rather
than waiting for the ordinary 12-second no-input timeout. It keeps waiting while
speech is recent or contact recognition is still decoding. Incomplete caller
thoughts retain their normal pause allowance. It clears abandoned recognition
fragments when retrying an empty interruption. Interrupted closing speech is
replayed before completion is finalized.

All gates, noise estimates, reference samples, and recognition activity belong to
one call. They do not contain persistent recordings. Added diagnostics report
echo suppression and empty-interruption recovery without duplicating caller
text. Routing, permission checks, emergency handoff, and fail-open behavior remain
in their existing deterministic code.

`scripts/smoke_noise_interruptions.py` exercises the actual Silero model with
synthetic road rumble, hiss, short clunks, clean/distorted playback echo, and
caller speech over noise or echo. The speech fixture comes from the existing
local speech benchmark, and no carrier call or staff message is sent. Passing
these checks does not establish performance for every car or speakerphone.
