# Floodman production hybrid voice stack

The production profile keeps Asterisk, Floodman's business knowledge,
customer records, Roomflow tools, scheduling rules, recordings, and
call history self-hosted.

It uses Deepgram Flux for listening, Groq-hosted Qwen for conversation
and tools, and ElevenLabs Flash v2.5 for natural speech.

Configure these secrets in `data/runtime.env` or Pterodactyl Startup:

```bash
export FLOODMAN_AI_PROFILE='production_hybrid'
export DEEPGRAM_API_KEY='...'
export GROQ_API_KEY='...'
export ELEVENLABS_API_KEY='...'
```

Never commit provider keys to GitHub.
