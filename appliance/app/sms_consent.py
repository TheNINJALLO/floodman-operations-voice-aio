from __future__ import annotations

import re


SMS_DISCLOSURE_VERSION = "2026-09-10-v1"
SMS_DISCLOSURE = (
    "By checking this box, I agree to receive recurring operational SMS alerts from Floodman Call Center "
    "about inbound customer calls, completed intakes, and emergency service requests. Message frequency "
    "varies. Message and data rates may apply. Reply STOP to opt out or HELP for help. Consent is not a "
    "condition of employment or purchase. See the SMS Terms and Privacy Policy."
)


def normalize_sms_phone(value: str) -> str:
    """Normalize a US staff mobile number to E.164 for Twilio delivery."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) != 11 or not digits.startswith("1"):
        raise ValueError("Enter a valid 10-digit US mobile number.")
    return "+" + digits
