# Local streaming voice trial

This is an opt-in CPU profile. It does not change the normal Deckard deployment
unless its image and environment are explicitly selected. Use a separate
`DATA_DIR` for evaluation. No cloud inference or paid AI keys are required.

| Function | Component |
| --- | --- |
| Live wording and approved knowledge | Pinned 8B Llama-family Q4_K_M through private llama.cpp |
| Streaming recognition | English Zipformer INT8 through Sherpa-ONNX |
| Speech detection and interruption | Per-call Silero state at 8 kHz |
| Contact details and recovery | Pinned Faster-Whisper small.en INT8 |
| Streaming voice | Pocket TTS 4.0.1, matching English model and preset states |
| Voice recovery | Existing local Kokoro, then its existing eSpeak fallback |
| Optional background briefings | Separate private Deckard API on loopback port 8082 |

The 8B download is not an Anthropic Claude release. Its exact publisher revision,
file size and checksum are in `config/haiku8b-model.json`. Speech pins, including
the voice states corresponding to the selected English checkpoint, are in
`config/local-streaming-assets.json`. Model weights are not baked into the image.

## Build

From the repository root:

```sh
docker build -f appliance/Dockerfile.local-trial -t floodman-local-trial appliance
```

The Dockerfile uses the previously verified CPU appliance by immutable digest
and installs the PyTorch CPU wheel explicitly. It adds no public AI or database
ports. The existing HTTPS website and authenticated voice previews remain
available when the image is run as a complete appliance.

Use `config/local-streaming-trial.env` as the profile reference. The default
image keeps optional Deckard briefings disabled until the full server trial is
accepted. The ordinary production image still defaults to Deckard and the
standard speech stack.

## Isolated evaluation

Use Python 3.12 and an isolated environment under `DATA_DIR`. Install the PyTorch
CPU wheel first, then the appliance's `streaming_trial` extra. For example, from
the `appliance` directory:

```sh
export DATA_DIR="$PWD/../data/local-voice-trial"
python -m venv "$DATA_DIR/venv"
"$DATA_DIR/venv/bin/pip" install torch==2.8.0 --index-url https://download.pytorch.org/whl/cpu
"$DATA_DIR/venv/bin/pip" install '.[dev,streaming_trial]'
"$DATA_DIR/venv/bin/python" scripts/download_local_trial.py --with-whisper
"$DATA_DIR/venv/bin/python" scripts/benchmark_local_speech.py --contacts --strict
```

On Windows, use `$env:DATA_DIR` and `venv/Scripts/python.exe`. The downloader
configures Hugging Face, Torch and temporary caches under `DATA_DIR`. The
5 GB GGUF and speech assets are reusable; a checksum mismatch cannot be accepted
as a completed download. `--speech-only` skips the GGUF when evaluating speech.
The benchmark generates only synthetic phrases and sends no carrier calls or
staff notifications. It writes listenable 8 kHz WAV files and a JSON report to
`DATA_DIR/benchmark`. Speech caching is disabled for the measurements.
The report separates first-attempt contact recognition accuracy from safe
readback/retry handling. A recognizer segment boundary is acceptable when the
actual intake engine keeps listening silently and preserves the continuation.
The temporary synthetic SQLite records stay local; published test artifacts
contain only synthetic WAV samples and JSON reports.

`--realtime` paces the synthetic input as a caller would. Without it,
recognition elapsed time measures compute throughput, not a caller's wait.
First-audio time is measured after models are loaded and is not end-to-end call
latency. The report includes the independent cold-start time.

`scripts/smoke_local_trial_llm.py` runs the pinned GGUF with the image's native
llama.cpp binary on loopback. It measures complete rewrites, exercises the real
1200 ms wording limit and prepared fallback, and checks exact transfer wording.
Run it separately from speech benchmarks on smaller test machines. None of
these tests establish production concurrency or Outreach-equivalent voice
quality.

## Call behavior

The recognizer emits partial text for timing diagnostics, never confirmed
customer facts. Turns use the current intake stage: ordinary conversation has a
600 ms minimum pause, confirmations retain the faster endpoint, and email keeps
its longer spelling pause. Unfinished fragments get additional breathing room.
Whisper decodes contact turns before the existing readback/confirmation rules.
Empty recognition does not immediately become a no-input prompt.

The voice engine streams whole phrases instead of regenerating each short
sentence independently. Audio conversion keeps filter history across chunks.
Readback speed uses ffmpeg tempo adjustment without changing pitch. A caller
interrupting playback stops the Pocket generator; cancelled audio is never
cached as a complete prompt. A generation failure before playback can use local
Kokoro. After partial playback, a failure is reported rather than restarting the
sentence as though nothing had been said.

Only ordinary detail questions can receive optional 8B wording changes. Changed
questions, newly introduced numbers, action claims, or responses beyond the
wording budget use the prepared question. Company introduction, contact
confirmations, emergency transfer, closing and protected wording retain their
deterministic paths. Routing, consent, suppression, verification, quiet hours,
DNC, reassignment and frequency decisions remain in application code.

The live-test profile sets `TEL_AGENT_NATURALIZE=false`: the same-host Ryzen
comparison measured 2.06 seconds for a median complete 8B rewrite, beyond the
1.2-second budget. Ordinary prepared questions therefore start without waiting
for a rewrite. Approved knowledge answers still use the selected local model.
Set the switch to `true` only for a controlled wording experiment with the budget
and validation safeguards retained.

## Deckard background service

On the server with sufficient RAM, set both:

```dotenv
LOCAL_DECKARD_BRIEFINGS=true
POST_CALL_BRIEFINGS_ENABLED=true
BRIEFING_BACKEND=local_api
BRIEFING_MODEL=floodman-deckard:40b-q4_k_m
BRIEFING_LLAMA_URL=http://127.0.0.1:8082/v1
BRIEFING_THREADS=4
BRIEFING_TIMEOUT_SECONDS=300
```

First boot verifies/downloads Deckard alongside the 8B weights. Supervisor starts
the optional Deckard process on loopback only. Briefing requests use
`BRIEFING_API_KEY`, defaulting to the local `LLAMA_API_KEY`. Existing scheduling
defers generation while calls are active and preempts drafts when a call starts.
Both models remain local; the 8B model handles live requests and Deckard handles
saved post-call drafts. A background-model outage cannot block the receptionist.

Selected-member notifications, local records and permission checks remain the
existing application workflow. Drafts do not choose recipients or authorize
business actions.

## Promotion

The dedicated GitHub workflow builds an immutable trial image, runs all tests,
downloads public models into a test-only volume, and checks real speech, 8B
inference and Asterisk handoff with networking disabled during inference.
Promotion to the live phone route still requires carrier tests for the greeting,
quiet and noisy callers, overlapping speech, spelling, corrections, completed
messages, active flooding and ordinary callback requests. Start with one call.
Keep the prior image and runtime configuration for rollback.
