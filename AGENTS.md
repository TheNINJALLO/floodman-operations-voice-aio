# Repository Instructions

- Preserve deterministic call routing and fail-open behavior.
- Never move consent, suppression, verification, quiet-hour, DNC, reassignment, or frequency decisions into prompts.
- Never accept a client-supplied `verified` Boolean for protected billing operations.
- Keep Roomflow routes configurable and idempotent.
- Write local business data before remote synchronization.
- Do not expose ARI, AMI, AudioSocket, local AI, or SQLite publicly.
- Add tests for every new call classification, business tool, and compliance rule.
- Keep all mutable runtime files under `DATA_DIR`.
