# Avery's conversation flow

The owner's supplied OutreachGenius campaign prompt informs the immediate
wording in `app/call_style.py` and the optional wording model in
`app/tel_agent_runtime.py`. Routine replies remain immediate: no model rewrite
is required before each question. The caller speaks to Avery, Floodman's
digital assistant; direct identity questions receive a truthful automated-system
description. The assistant and simulator pages use the same name.

Applied conversation goals:

- Calm, friendly, concise wording, one question per turn, and a brief handoff.
- Ask for first and last name. If only one name is given, ask for the missing
  last name once. Refusal or a mononym is accepted without inventing a surname.
- A negative confirmation means the candidate is wrong. It does not decline
  collection. Clear the rejected address and its service-area result, collect
  the correction, then wait for confirmation. Explicit refusals still work;
  two rejected addresses are marked unavailable for staff follow-up.
- Accept an explicitly introduced corrected address later in the call,
  including a street number spoken as individual digits, and reconfirm it.
- Spell the email local part at normal speed, with the domain spoken naturally.
  The stored address remains exact and unconfirmed until the caller agrees.
- Ask the anything-else question once, acknowledge extra details without
  repeating it, answer questions, and let the caller finish. Holds, unfinished
  thoughts, and farewell corrections remain possible.
- Close warmly with the callback timeframe, thanks, and a brief personal
  farewell. Do not recite the message back at the end.

Existing deterministic emergency routing, business facts, account verification,
and compliance controls retain their authority. Conflicting historical campaign
instructions do not change those decisions.
