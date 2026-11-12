"""Approved, editable answers for common caller phrasings; no business actions."""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter, defaultdict
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.company_conversation import safe_answer
from app.intake import classify_service, normalized

logger = logging.getLogger(__name__)
SERVICES = Literal["", "water_damage_restoration", "basement_waterproofing", "foundation_repair",
                   "crawl_space_encapsulation", "mold_remediation", "sump_pump_and_drainage"]
CATEGORIES = {"water_damage": "water_damage_restoration", "waterproofing": "basement_waterproofing",
              "foundation": "foundation_repair", "crawl_space": "crawl_space_encapsulation",
              "mold": "mold_remediation", "sump_drainage": "sump_pump_and_drainage"}


def source_digest(body: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", body).strip().encode("utf-8")).hexdigest()


def wording(text: str) -> str:
    """Allow social framing and spelling variants, retaining meaning and negation."""
    words = normalized(text)
    words = re.sub(r"\byou (?:guys|guise)\b|\by all\b", "you", words)
    words = re.sub(r"\bmould\b", "mold", words)
    words = re.sub(r"\bcrawlspace\b", "crawl space", words)
    words = re.sub(r"^(?:(?:oh|well|um|uh|hey|hello|hi|okay|ok|so|and|also|but|please|thanks|thank you) )+", "", words)
    words = re.sub(r"^i (?:(?:was|am|m) )?(?:just )?(?:wondering|curious|wondered|wanted to (?:ask|know)) (?:if |whether )?", "", words)
    words = re.sub(r" (?:please|thanks|thank you)$", "", words)
    return words


class PreparedAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,79}$")
    service: SERVICES = ""
    source: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,79}\.md$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    questions: list[str] = Field(min_length=1, max_length=12)
    followups: list[str] = Field(default_factory=list, max_length=8)
    answer: str = Field(min_length=10, max_length=600)

    @field_validator("questions", "followups")
    @classmethod
    def valid_phrasings(cls, values):
        if any(not wording(value) or len(value) > 320 for value in values):
            raise ValueError("Invalid question phrasing")
        return values

    @field_validator("answer")
    @classmethod
    def valid_answer(cls, value):
        if not safe_answer(value) or "?" in value:
            raise ValueError("A prepared answer must not perform actions or ask intake questions")
        return value.strip()


class PreparedAnswers:
    def __init__(self, documents):
        sources = defaultdict(list)
        for document in documents:
            if document.category != "prepared_answers":
                sources[document.path.name].append(document)
        records = []
        self.invalid_entries = 0
        for document in documents:
            if document.category != "prepared_answers":
                continue
            if len(document.body) > 64000:
                self.invalid_entries += 1
                continue
            sections = re.split(r"(?m)^## [^\n]+\n", document.body)[1:]
            for section in sections[:80]:
                try:
                    entry = PreparedAnswer.model_validate(yaml.safe_load(section))
                    source = sources.get(entry.source, [])
                    if len(source) != 1 or source_digest(source[0].body) != entry.source_sha256:
                        self.invalid_entries += 1
                        continue
                    records.append(entry)
                except (yaml.YAMLError, ValidationError, ValueError, TypeError):
                    self.invalid_entries += 1
        counts = Counter(entry.id for entry in records)
        self.entries = tuple(entry for entry in records if counts[entry.id] == 1)
        self.questions = defaultdict(list)
        self.followups = defaultdict(list)
        for entry in self.entries:
            for phrase in set(map(wording, entry.questions)):
                self.questions[phrase].append(entry)
            for phrase in set(map(wording, entry.followups)):
                self.followups[phrase].append(entry)
        if self.invalid_entries:
            logger.warning("Prepared company answers skipped %d invalid or stale entries", self.invalid_entries)

    def match(self, text: str, *, topic: str = "", description: str = "") -> PreparedAnswer | None:
        key = wording(text)
        # No fuzzy token similarity: cost, negation, guarantees and health
        # questions must not borrow a reassuring answer to a different question.
        direct = self.questions.get(key, [])
        if direct:
            return direct[0] if len(direct) == 1 else None
        service = classify_service(text).get("service_key", "")
        if not service:
            service = CATEGORIES.get(topic, topic) if topic in CATEGORIES or topic in CATEGORIES.values() else ""
        if not service:
            service = classify_service(description).get("service_key", "")
        followups = [entry for entry in self.followups.get(key, []) if not entry.service or (service and entry.service == service)]
        return followups[0] if len(followups) == 1 else None
