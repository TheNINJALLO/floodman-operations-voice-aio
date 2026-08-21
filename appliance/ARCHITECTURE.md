# Architecture

```text
Twilio / SIP carrier
        │ SIP + RTP
        ▼
┌──────────────────────────────────────────────────────────┐
│ Floodman Voice Appliance                                │
│                                                          │
│ Asterisk ── AudioSocket PCM 8 kHz ── Floodman Voice Core │
│                                          │               │
│                 ┌────────────────────────┼─────────────┐ │
│                 ▼                        ▼             ▼ │
│       Faster-Whisper CPU       Qwen3-4B / A1000   Kokoro CPU
│                 │                        │             │ │
│                 └──── deterministic intake state ─────┘ │
│                                  │                       │
│                         SQLite / Knowledge / SMS         │
│                                  │                       │
│                            FastAPI web panel             │
└──────────────────────────────────────────────────────────┘
```

## Trust boundaries

- The LLM and AudioSocket bind only to loopback.
- ARI and AMI are disabled.
- The web panel uses the admin token in an HttpOnly cookie.
- SIP, RTP, and the panel are the only public surfaces.
- Knowledge documents marked `approved: false` are never used for answers.
- The model cannot directly send SMS, transfer a call, finish intake, or choose the next question.

## Failure behavior

| Failure | Customer behavior |
|---|---|
| Qwen unavailable | Deterministic field parsing continues where possible; no model-written narration |
| Faster-Whisper error | One short request to repeat |
| Kokoro error | eSpeak fallback |
| Both voices fail | Call closes and partial intake is retained |
| Caller hangs up | Partial team alert, idempotently |
| Emergency words | Partial alert and optional emergency transfer |
| Invalid runtime.env | Startup stops before secrets or duplicates are written |

## Capacity target

Version 0.1.0 is engineered for one concurrent live call. More concurrency should be measured on the exact A1000 host before raising `--parallel` or context size.
