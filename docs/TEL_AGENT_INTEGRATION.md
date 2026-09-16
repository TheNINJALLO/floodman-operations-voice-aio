# Tel-Agent replacement engine

Floodman uses Tel-Agent's pinned streaming STT, LLM, TTS, and spoken-turn driver over
Asterisk's existing private AudioSocket path. LiveKit is not part of this deployment.
`VOICE_ENGINE=tel_agent` replaces the appliance call loop itself; it is not only a
provider adapter.

## Runtime behavior

- `VOICE_ENGINE=tel_agent` makes Tel-Agent primary for inbound and outbound calls.
- One Deepgram connection remains open for the call. Stable recognition segments are
  accumulated until the provider marks the utterance complete.
- Tel-Agent streams model tokens into phrase-sized TTS requests. The first phrase can
  start playing while the rest of the response is still being generated.
- Local VAD stops model generation, TTS production, and AudioSocket playback when the
  caller interrupts. Recognition remains active while the assistant is speaking.
- The durable assistant transcript is committed after playback and contains only the
  phrases Tel-Agent reports as spoken, including interrupted turns.
- The AudioSocket listener binds only to loopback (`127.0.0.1:8090` standalone and
  `127.0.0.1:8091` in the unified image).
- Asterisk waits for the authenticated appliance `/ready` endpoint before accepting
  media. AudioSocket failure follows the existing deterministic technical-failure and
  callback path instead of starting a second AI agent.
- Only final Deepgram turns are written to the local Floodman event store. Partial STT
  hypotheses are never archived. Stable segments are accumulated until Deepgram marks
  the complete utterance (`speech_final`), preventing a long customer explanation from
  becoming multiple agent interruptions. The gate's opening transcript is retained as
  the first caller turn, and each turn is idempotent by call ID and sequence.
- Business events use the signed loopback outbox. Verification, opt-out, compliance,
  availability, and protected billing decisions remain in deterministic Floodman code
  rather than model instructions.

## Feature ownership

Tel-Agent's phone-conversation features run inside the Voice appliance, while existing
Floodman services remain the source of truth:

- Tel-Agent conversation archive and searchable transcripts map to Floodman Calls and
  the shared customer timeline.
- `take_message` maps to the durable local intake plus team notification workflow.
- knowledge search maps to Floodman's approved knowledge library.
- call transfer maps to the deterministic Asterisk action registry.
- webhooks and HTTP integrations map to the signed, loopback-only Business Suite outbox.
- contacts map to the canonical Floodman customer record rather than a second contact
  database.

Calendar availability and irreversible scheduling remain Business Suite operations.
The phone agent may collect a requested window, but it cannot promise or create an
appointment until the deterministic Business Suite workflow authorizes that action.

## Required configuration

```dotenv
VOICE_ENGINE=tel_agent
TEL_AGENT_DEEPGRAM_MODEL=nova-3
TEL_AGENT_ENDPOINTING_MS=450
TEL_AGENT_UTTERANCE_END_MS=1000
TEL_AGENT_NATURALIZE=true
DEEPGRAM_API_KEY=
GROQ_API_KEY=
ELEVENLABS_API_KEY=
ELEVENLABS_VOICE_ID=
```

The existing production variables `GROQ_LLM_MODEL`, `ELEVENLABS_TTS_MODEL`, and
`STT_LANGUAGE` are reused. `TEL_AGENT_DEEPGRAM_MODEL` may override the pinned
Tel-Agent default. Provider secrets belong in `${DATA_DIR}/runtime.env` or the server
environment and must not be committed.

For Groq Qwen 3 models, the adapter preserves Floodman's phone profile by sending
`reasoning_effort=none` and `reasoning_format=hidden`; it does not add those
Groq-specific fields to other OpenAI-compatible endpoints.

Deployment rollback uses the retained previous immutable image digest. The removed call
loop is not kept active beside Tel-Agent.

Tel-Agent is currently an alpha project. Floodman exposes provider/runtime status at
`/ready`; `streaming_stt` and `streaming_tts` report whether the cloud full-duplex path
is active. When those keys are absent or a provider fails before a response starts,
the call remains available through private local Whisper, llama.cpp, and Kokoro.
