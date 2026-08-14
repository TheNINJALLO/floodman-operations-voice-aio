# Complete customer intake, transcript recovery, and team alerts

Floodman inbound calls use a durable intake snapshot. Ava saves the known information after every substantive caller answer, then the post-call hook attaches the final summary and full transcript.

## Conversation flow

Ava first lets the caller explain the problem, classifies the requested service against Floodman's approved service catalog, and then asks only the relevant unanswered questions. The intake covers:

- Full name, callback number, email or an explicit declined/unavailable status, and full property address
- Requested service and whether it is supported, unsupported, or requires human review
- Detailed description, affected areas, probable source, start time, and whether the condition is active or worsening
- Property type and caller relationship
- Standing water, sewage or contamination, electrical hazards, structural concerns, and safe occupancy or access
- Insurance or previous work, available photos or video, referral source, urgency, and preferred callback time

Ava never schedules an inspection from this inbound flow. When a complete intake is saved, she tells the caller that the information was forwarded and that the team will call within 24 hours. Emergency language remains more urgent and does not promise an exact arrival time.

## Unsupported services

The classifier first checks explicit out-of-scope categories and then Floodman's approved service catalog. Ava clearly states when Floodman does not currently offer a requested service. She still collects the contact and issue details, creates a callback task, records the request as unsupported, and sends the team alert. An unrecognized request is marked for review instead of being falsely accepted.

## Interrupted calls

The post-call hook finalizes inbound customer calls even when the caller hangs up, the inactivity watchdog ends the call, or an AI provider fails. It stores the full transcript, preserves any earlier structured snapshot, creates a callback task when a valid number is available, and queues one idempotent team SMS containing the recovered information and a transcript excerpt.

An immediate hangup with caller ID is still treated as a recoverable call. The team alert contains the number, call status, call ID, and the web-app transcript pointer even when no other facts were spoken.

## Web app

Open **Calls & Intake** in Voice AIO. The list shows complete, unsupported, review, and partial calls. **View details** opens the structured intake, team-alert status, full role-labeled transcript, and chronological call events.

## Team SMS

The message includes the caller's recovered name, number, email status, address, requested service, service-review result, detailed issue, property and safety context, timing, insurance context, evidence, urgency, call status, callback expectation, and call ID. The full transcript remains available in the web app.
