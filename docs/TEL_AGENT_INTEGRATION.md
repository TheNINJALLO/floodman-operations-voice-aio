# Tel-Agent replacement engine

Floodman uses Tel-Agent's pinned streaming STT, LLM/tool, TTS, and spoken-turn
components over Asterisk's existing private AudioSocket path. LiveKit is not part of
this deployment.

## Runtime behavior

- `VOICE_ENGINE=tel_agent` makes Tel-Agent primary for inbound and outbound calls.
- The AudioSocket listener binds only to `127.0.0.1:9020` by default.
- A readiness marker under `${DATA_DIR}/runtime/tel-agent-ready` is created only after
  the listener and required provider configuration are ready.
- When that marker is absent or AudioSocket cannot connect, the dialplan routes to
  the existing AVA engine. A provider failure after connection uses the authenticated
  control plane and server-owned channel metadata to redirect the same call into AVA;
  Floodman reception remains the final configured recovery route if that redirect fails.
- Only final Deepgram turns are written to the local Floodman event store. Partial STT
  hypotheses are never archived. Stable segments are accumulated until Deepgram marks
  the complete utterance (`speech_final`), preventing a long customer explanation from
  becoming multiple agent interruptions. The gate's opening transcript is retained as
  the first caller turn, and each turn is idempotent by call ID and sequence.
- Business tools call authenticated loopback endpoints. Verification, opt-out,
  compliance, availability, and protected billing decisions remain in deterministic
  Floodman code rather than model instructions.

## Required configuration

```dotenv
VOICE_ENGINE=tel_agent
TEL_AGENT_ENABLED=true
TEL_AGENT_HOST=127.0.0.1
TEL_AGENT_PORT=9020
TEL_AGENT_DEEPGRAM_MODEL=nova-3
TEL_AGENT_ENDPOINTING_MS=450
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

AVA remains installed as a recovery engine during rollout. Set `VOICE_ENGINE=ava` to
perform an immediate rollback without rebuilding the image.
