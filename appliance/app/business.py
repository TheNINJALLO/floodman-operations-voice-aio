from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.intake import SUPPORTED_ALIASES, UNSUPPORTED_ALIASES, normalized, phrase_match

SERVICE_NAMES = {
    "water_damage_restoration": "water-damage restoration",
    "mold_remediation": "mold remediation",
    "foundation_repair": "foundation repair",
    "basement_waterproofing": "basement waterproofing",
    "crawl_space_encapsulation": "crawl-space encapsulation",
    "sump_pump_and_drainage": "sump-pump and drainage services",
}


@dataclass(slots=True)
class ServiceAreaResult:
    status: str
    city: str
    message: str


class BusinessDirectory:
    def __init__(self, service_area_path: Path):
        self.path = service_area_path
        self.cities: set[str] = set()
        self.reload()

    def reload(self) -> None:
        if self.path.exists():
            raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
            cities = raw.get("cities") or []
            self.cities = {str(city).strip().lower() for city in cities if str(city).strip()}

    def service_area(self, address_or_city: str) -> ServiceAreaResult:
        text = normalized(address_or_city)
        matches = [city for city in self.cities if re.search(rf"(?:^| ){re.escape(normalized(city))}(?:$| )", text)]
        if matches:
            city = max(matches, key=len).title()
            return ServiceAreaResult("published", city, f"{city} is in Floodman's published service area. Final property and project eligibility still needs confirmation.")
        return ServiceAreaResult("manual_confirmation", "", "That location is not in my approved city list yet. I will include the full address so the team can confirm service availability.")

    def direct_answer(self, query: str) -> str:
        text = normalized(query)
        if any(term in text for term in ("what services", "what do you do", "kind of work", "services do you offer")):
            return "Floodman provides water-damage restoration, basement waterproofing, foundation repair, crawl-space encapsulation, mold remediation, and sump-pump and drainage services."
        if any(term in text for term in ("free inspection", "inspection free", "free consultation")):
            return "Floodman's website advertises free inspections and consultations. Final recommendations and pricing depend on the property conditions and the work required."
        if any(term in text for term in ("how much", "price", "cost", "estimate over the phone")):
            return "Pricing depends on the source and extent of the problem, affected materials, access, and the inspection findings. The team can review the next step after collecting the property details."
        if any(term in text for term in ("warranty", "guarantee")):
            return "Warranty coverage depends on the installed system and the signed agreement. The Floodman team will need to review the specific warranty."
        if any(term in text for term in ("insurance", "direct billing", "deductible")):
            return "Floodman can document work when applicable, but coverage and claim decisions belong to the insurer. The team can confirm billing or documentation options for the specific job."
        if any(term in text for term in ("open 24", "after hours", "emergency", "24 7")):
            return "Floodman advertises 24/7 emergency intake and water-damage response."
        if "serve" in text or "service area" in text or "come to" in text:
            result = self.service_area(query)
            if result.status == "published":
                return result.message
        for key, aliases in SUPPORTED_ALIASES.items():
            if any(phrase_match(text, phrase) for phrase in aliases):
                return f"Yes. Floodman advertises {SERVICE_NAMES[key]}. The exact recommendation and scope require a property inspection."
        for aliases in UNSUPPORTED_ALIASES.values():
            if any(phrase_match(text, phrase) for phrase in aliases):
                return "That is not a service Floodman advertises, but I can still collect the details and send them to the team for review."
        return ""
