# Google Call Gate

## Why it exists

A normal voice agent can greet or interrupt a prerecorded forwarding notice, treat the words “Call from Google” as the customer’s request, or miss the real caller’s first sentence. The Floodman gate handles the opening before the conversational agent speaks.

## Signals

The classifier combines:

- opening transcript
- incoming DID
- configured Google DID list
- source hint
- announcement phrases
- automated-business-call phrases
- suspicious credential or payment language
- live-customer language after the announcement
- elapsed gate time

Caller ID alone is not trusted as proof that a call came from Google.

## Routes

### Direct customer

Natural caller speech without an announcement routes to `floodman_inbound`. The opening speech is preserved in the gate record and returned by the pre-call hook. A direct caller who waits silently for Floodman to greet them is failed open after `GATE_NO_SPEECH_TIMEOUT_SECONDS`, default 3.5 seconds, instead of waiting through the full Google-announcement window.

### Google-forwarded customer

A forwarding or recording notice places the gate into `WAITING_FOR_HUMAN`. Once customer speech follows, the notice is stripped from the opening transcript and the call routes to the ordinary inbound agent.

### Google automated business call

An automated caller asking about public hours, services, availability, or booking routes to `floodman_google_business`. This agent can only use approved public-information, service-area, availability, scheduling, and disposition tools.

### Suspicious Google claim

Payment, credential, one-time-code, remote-access, listing-pressure, or keypad-verification language routes to `floodman_security`. The security agent records the event and does not share secrets or press keys.

### Timeout or error

The gate routes to ordinary customer service. A classification failure must never become an automatic hang-up. The shorter no-speech fallback applies only when no announcement or usable speech is detected; a recognized Google forwarding announcement can continue through the full gate window while the system waits for the customer.

## Pattern maintenance

Patterns are in:

```text
app/call_gate/patterns.py
```

Add a phrase only after reviewing real call audio or a verified transcript. Avoid overly broad words that would classify ordinary customers as Google calls.

## Shadow-mode validation

Before allowing the gate to control routing:

1. Forward a test DID through the same Google path as the production number.
2. Save gate decisions and compare them with the actual call source.
3. Test different cellular carriers and codecs.
4. Test a caller who speaks immediately after the announcement.
5. Test silence, noise, accented speech, and a clipped announcement.
6. Test an automated Google information call.
7. Test an internal simulated scam call.
8. Confirm every uncertain case reaches the normal inbound agent.

The `POST /api/v1/gate/classify` endpoint and dashboard classifier form are useful for transcript fixtures, but they do not replace real audio-path tests.
