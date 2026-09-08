from __future__ import annotations

import re
from typing import Any

PHONE_RE = re.compile(r"\+[1-9][0-9]{7,14}$")
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
SPELLED_SEPARATOR_RUN_RE = re.compile(r"(?<![a-z0-9])(?:[a-z0-9]\s*(?:-|,)\s*)+[a-z0-9](?![a-z0-9])")

SUPPORTED_ALIASES: dict[str, tuple[str, ...]] = {
    "water_damage_restoration": (
        "water damage", "water mitigation", "flood cleanup", "flooding", "flooded basement",
        "water extraction", "burst pipe", "water heater leak", "active leak",
        "standing water", "wet basement",
    ),
    "mold_remediation": ("mold", "mould", "mold remediation", "musty odor", "microbial growth"),
    "foundation_repair": ("foundation", "bowing wall", "bulging wall", "settlement", "structural crack", "carbon fiber"),
    "basement_waterproofing": ("waterproofing", "basement seepage", "wall seepage", "floor seepage", "drain tile", "moisture management"),
    "crawl_space_encapsulation": ("crawl space", "crawlspace", "encapsulation", "vapor barrier", "crawl space moisture"),
    "sump_pump_and_drainage": ("sump pump", "sump-pump", "drainage", "discharge line", "pit overflowing", "pump alarm"),
}

UNSUPPORTED_ALIASES: dict[str, tuple[str, ...]] = {
    "roofing": ("roof", "roofs", "roof repair", "roofing", "replace roof", "shingle"),
    "plumbing": ("plumber", "plumbing repair", "fix faucet", "replace pipe", "sewer line repair"),
    "electrical": ("electrician", "electrical repair", "rewire", "breaker repair"),
    "hvac": ("hvac", "furnace repair", "air conditioner", "ac repair", "duct cleaning"),
    "septic": ("septic pumping", "septic repair", "septic tank"),
    "pest_control": ("pest control", "termite", "exterminator", "rodent removal"),
    "general_remodeling": ("kitchen remodel", "bathroom remodel", "handyman", "painting only", "drywall only"),
    "landscaping": ("landscaping", "tree removal", "lawn care", "snow removal"),
    "specialty_hazard": ("asbestos", "lead paint", "biohazard"),
}

AFFIRMATIVE = {"yes", "yeah", "yep", "correct", "right", "that's right", "that is right", "sounds right", "uh huh", "sure"}
NEGATIVE = {"no", "nope", "wrong", "incorrect", "not right", "that's wrong", "change it", "correction"}


def clean(value: Any, maximum: int = 12000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def normalized(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", clean(value).lower()).strip()


def phrase_match(text: str, phrase: str) -> bool:
    candidate = normalized(phrase)
    return bool(candidate and re.search(rf"(?:^| ){re.escape(candidate)}(?:$| )", text))


def classify_service(description: Any) -> dict[str, str]:
    text = normalized(description)
    supported: list[tuple[int, str]] = []
    unsupported: list[tuple[int, str]] = []
    for key, phrases in SUPPORTED_ALIASES.items():
        score = sum(max(1, len(normalized(p).split())) for p in phrases if phrase_match(text, p))
        if score:
            supported.append((score, key))
    for key, phrases in UNSUPPORTED_ALIASES.items():
        score = sum(max(1, len(normalized(p).split())) for p in phrases if phrase_match(text, p))
        if score:
            unsupported.append((score, key))
    if supported and (not unsupported or max(supported)[0] >= max(unsupported)[0]):
        return {"service_status": "supported", "service_key": max(supported)[1]}
    if unsupported:
        return {"service_status": "unsupported", "service_key": max(unsupported)[1]}
    return {"service_status": "review", "service_key": ""}


def normalize_confirmation(value: Any) -> str:
    text = normalized(value)
    if text in AFFIRMATIVE or text.startswith("yes ") or text.startswith("correct "):
        return "yes"
    if text in NEGATIVE or text.startswith("no ") or text.startswith("wrong "):
        return "no"
    return ""


def classify_property_context(value: Any) -> str:
    text = normalized(value)
    residential = ("home", "house", "residential", "my residence", "homeowner")
    managed = ("business", "commercial", "office", "store", "rental property", "property manager")
    # Keep this correction scoped to the home/business question. Whisper can
    # render a short telephone answer of "home" as "hope".
    if text == "hope":
        return "Residential property"
    if any(term in text for term in residential):
        return "Residential property"
    if any(term in text for term in managed):
        return "Commercial or managed property"
    return ""


def normalize_phone(value: Any, *, default_country: str = "1") -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 10:
        digits = default_country + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    return ""


def normalize_email(value: Any) -> str:
    text = clean(value, 320).lower()
    # Whisper can render a slowly spelled sequence such as "j o s h" as
    # "j-o-s-h" even when the caller never said "dash". It may also insert a
    # comma between two spelled runs, as in "j-o-s-h, s-h". Collapse only runs
    # of individual characters before translating an explicitly spoken "dash"
    # so legitimate names such as "mary-jane" remain intact.
    text = SPELLED_SEPARATOR_RUN_RE.sub(lambda match: re.sub(r"[\s,-]+", "", match.group(0)), text)
    replacements = {
        " at ": "@", " dot ": ".", " underscore ": "_", " dash ": "-", " hyphen ": "-",
        " period ": ".", " gmail com": "gmail.com", " yahoo com": "yahoo.com",
        " outlook com": "outlook.com", " hotmail com": "hotmail.com",
    }
    padded = f" {text} "
    for old, new in replacements.items():
        padded = padded.replace(old, new)
    email = re.sub(r"\s+", "", padded.strip()).rstrip(".,;:!?")
    return email if EMAIL_RE.fullmatch(email) else ""


def normalize_name(value: Any) -> str:
    text = clean(value, 200).strip(" .,;:!?\t\r\n")
    text = re.sub(
        r"^(?:my name is|this is|it is|it's|you can put (?:it )?under|put (?:it )?under)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return clean(text, 120).strip(" .,;:!?\t\r\n")


def spoken_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return ", ".join(" ".join(group) for group in (digits[:3], digits[3:6], digits[6:]))
    return " ".join(digits)


def spoken_email(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("_", " underscore ").replace("-", " dash ").replace("@", " at ").replace(".", " dot ")).strip()


def detect_emergency(value: Any) -> bool:
    text = normalized(value)
    terms = (
        "water rising", "actively rising", "sewage", "electrical", "electricity",
        "electrical panel", "sparking", "gas odor", "gas smell", "collapse",
        "falling", "unsafe to enter", "structural danger", "live wire", "emergency",
    )
    return any(term in text for term in terms)


def human_requested(value: Any) -> bool:
    text = normalized(value)
    return any(term in text for term in ("human", "representative", "real person", "someone on the team", "operator", "transfer me"))
