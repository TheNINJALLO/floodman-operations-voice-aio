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
│       Faster-Whisper CPU       Deckard 40B Q4     Kokoro CPU
│                 │                        │             │ │
│                 └──── deterministic intake state ─────┘ │
│                                  │                       │
│                   SQLite / Knowledge / SMS / Email       │
│                                  │                       │
│                            FastAPI web panel             │
└──────────────────────────────────────────────────────────┘
```

## Trust boundaries

- The LLM and AudioSocket bind only to loopback.
- ARI and AMI are disabled.
- The web panel uses salted scrypt password hashes, role-based accounts, revocable server-side sessions, strict HttpOnly cookies, and CSRF tokens. The admin token is retained only for short-lived recovery access.
- Browser subscriptions use a persistent VAPID key under `DATA_DIR/push`; push previews contain no caller PII and link back to authenticated records.
- SMTP credentials are stored in a restricted persistent file, masked from browser responses, and used only for active users whose role and per-event preferences permit email delivery.
- Administrators select from the voices reported by the installed Kokoro bundle. The persisted voice and speed are applied locally, and preview synthesis is blocked during active calls.
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
| SMTP or SMS unavailable | Delivery failure recorded; live conversation continues without waiting |
| Invalid runtime.env | Startup stops before secrets or duplicates are written |

## Capacity target

Start with one concurrent live call. CPU inference is the default; CUDA is optional via `LOCAL_AI_DEVICE=cuda`. Measure recognition, model, and synthesis latency on the actual host and panel allocation before raising concurrency or context size. `LOCAL_AI_ONLY=true` ignores retained cloud provider credentials. Local recognition uses endpointed audio, so interruption handling and silence thresholds still require carrier-call validation.
