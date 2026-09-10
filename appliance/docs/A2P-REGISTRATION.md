# Floodman A2P 10DLC registration

This file contains the production campaign wording implemented by the Floodman Call Center. It does not replace the legal business identity, authorized representative attestation, fees, or review performed in Twilio.

## Registration prerequisites

- Use a paid Twilio account. Trial accounts cannot register.
- Complete or select the Primary Customer Profile that matches the business's legal records.
- Register the Brand using the exact legal name, EIN, entity type, address, website, and authorized representative details.
- Create or select a Messaging Service and add the SMS-capable US 10DLC number to its Sender Pool.
- If the approved Brand is a Sole Proprietor Brand, use the required Sole Proprietor use case. Otherwise, use Low Volume Mixed for this low-volume mix of operational call events unless Twilio presents a more specific eligible use case.

## Campaign fields

Campaign description:

> Floodman Call Center sends low-volume operational SMS alerts to Floodman team members who individually opt in from their authenticated user profile. Alerts cover new inbound customer calls, completed call intake records, emergency service requests, human-transfer requests, and incomplete calls that need follow-up. Messages identify Floodman Call Center and link the recipient to the secure call workspace. The campaign does not send third-party marketing.

Message flow:

> Floodman team members opt in at https://aicall.oninetwork.com/profile after signing in to their individual account. The user enters their own US mobile number and checks a previously unchecked SMS consent box that states the brand, recurring operational message types, variable frequency, possible message and data rates, STOP and HELP instructions, and that consent is not a condition of employment or purchase. The application records the user, normalized mobile number, consent time, source, exact disclosure, and disclosure version. Administrators cannot opt in another user. Users opt out by replying STOP or by clearing the SMS checkbox in their profile. The publicly accessible program description is https://aicall.oninetwork.com/sms-program, terms are https://aicall.oninetwork.com/terms, and privacy policy is https://aicall.oninetwork.com/privacy.

Sample message 1:

> Floodman Call Center: A new inbound call connected. Sign in securely: https://aicall.oninetwork.com/calls/[call_id] Reply STOP to opt out or HELP for help.

Sample message 2:

> Floodman Call Center: An emergency service intake needs immediate review. Sign in securely: https://aicall.oninetwork.com/calls/[call_id] Reply STOP to opt out or HELP for help.

Other campaign settings:

- Messages include embedded links: Yes
- Messages include embedded phone numbers: No
- Age-gated content: No
- Direct lending or loan arrangement: No
- Opt-in keywords and opt-in message: Leave blank because SMS keyword opt-in is not offered.
- Opt-out/help handling: Use Twilio's default or Advanced Opt-Out feature for STOP and HELP.

## Production activation

After Twilio approves the Campaign, copy the real credentials and sender identifiers into the protected Pterodactyl variables. Do not commit them:

- `TWILIO_ACCOUNT_SID`
- either `TWILIO_API_KEY` and `TWILIO_API_KEY_SECRET`, or `TWILIO_AUTH_TOKEN`
- `TWILIO_MESSAGING_SERVICE_SID`
- `TWILIO_SMS_FROM_NUMBER` only when no Messaging Service is used

Then restart the shared server, enroll a test user's own number from **Your profile**, and verify an alert's delivery record before enrolling other users.
