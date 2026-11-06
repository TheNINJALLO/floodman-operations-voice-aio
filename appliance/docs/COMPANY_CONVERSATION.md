# Company questions and conversational context

Company questions can be asked before or during intake. Avery recognizes direct
questions and conversational forms such as “tell me about” and “does that help.”
Answers leave the active intake stage unchanged. A caller can continue asking
questions or give the requested detail without hearing the checklist again.
An explicit request for an estimate or inspection can still begin intake.

Each call retains at most four company question/answer pairs under its saved
conversation metadata. Short follow-ups use the previous topic, recent answers,
and the caller's reported concern to resolve “it” and “that.” A new explicitly
named service starts a new retrieval query. This context is never treated as
proof of company facts, and it is not shared across callers.

Common policy answers remain deterministic and immediate. Detailed answers use
approved knowledge documents and the local 8B model, with a four-second answer
deadline and a short output limit. The model receives a calm, professional,
caring speaking style and does not collect contacts or change call stages.
Outputs claiming bookings, verification, transfers, charges or invented prices
are rejected. Missing answers receive a brief team-confirmation response.
Source routing is logged as `company_answer` events without customer text.

The new `01-company-conversation.md` contains company background from the owner's
OutreachGenius brief and the existing approved service/inspection facts. For an
existing deployment it must be added to `KNOWLEDGE_DIR` without overwriting an
operator-edited document; the release deployment does this after backup.
Company information is not inferred from arbitrary customer conversations.

Caller reports of worry or frustration receive a short, steady acknowledgment,
without cheerful filler or claims of human feelings. Routing, emergency
decisions, consent, suppression and protected business operations remain in code.
Voice presets affect delivery; they do not provide a guaranteed emotion setting.
