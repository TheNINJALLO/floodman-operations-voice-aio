from __future__ import annotations

import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

_STOP_WORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "by", "can", "could",
    "do", "does", "for", "from", "had", "has", "have", "how", "i", "if", "in",
    "is", "it", "me", "my", "of", "on", "or", "our", "that", "the", "their",
    "there", "they", "this", "to", "us", "was", "we", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
}

# Query expansion keeps the search lightweight while covering common ways callers describe
# the same property problem. These are retrieval hints, not business facts.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "basement": ("cellar", "lowerlevel"),
    "bowing": ("bulging", "leaning", "wall"),
    "burst": ("broken", "pipe", "water"),
    "crawlspace": ("crawl", "space", "encapsulation"),
    "crawl": ("crawlspace", "encapsulation"),
    "crack": ("foundation", "wall", "floor", "leak"),
    "damp": ("moisture", "wet", "humidity"),
    "drain": ("drainage", "water", "sump"),
    "emergency": ("urgent", "flood", "water", "sump"),
    "encapsulation": ("crawlspace", "vapor", "moisture"),
    "estimate": ("inspection", "pricing", "price", "quote"),
    "flood": ("water", "damage", "extraction", "emergency"),
    "foundation": ("structural", "crack", "bowing", "settlement"),
    "leak": ("water", "seepage", "wet", "basement"),
    "mildew": ("mold", "odor", "moisture"),
    "mold": ("mildew", "remediation", "containment", "moisture"),
    "musty": ("odor", "mold", "moisture", "crawlspace"),
    "price": ("pricing", "estimate", "inspection", "cost"),
    "quote": ("estimate", "pricing", "inspection"),
    "settling": ("settlement", "foundation", "movement"),
    "sump": ("pump", "drainage", "emergency", "water"),
    "waterproofing": ("basement", "leak", "drainage", "moisture"),
    "wet": ("water", "leak", "moisture", "damp"),
    "warranty": ("coverage", "agreement", "guarantee"),
}


def _tokens(value: str) -> list[str]:
    return [token for token in _TOKEN_RE.findall(value.lower()) if token not in _STOP_WORDS]


def _expanded_query_tokens(value: str) -> Counter[str]:
    result: Counter[str] = Counter(_tokens(value))
    for token in tuple(result):
        for synonym in _SYNONYMS.get(token, ()):
            result[synonym] += 1
    return result


def _normal(value: str) -> str:
    value = value.lower().replace("&", " and ")
    return " ".join(_TOKEN_RE.findall(value))


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    path: Path
    title: str
    category: str
    tags: tuple[str, ...]
    approved: bool
    source_url: str
    reviewed_at: str
    summary: str
    body: str
    token_counts: Counter[str]
    title_counts: Counter[str]
    tag_counts: Counter[str]
    mtime_ns: int
    size: int

    @property
    def public_path(self) -> str:
        return self.path.as_posix()


class KnowledgeBase:
    """Search approved Floodman Markdown without an external vector database.

    The library is intentionally deterministic and CPU-light. It watches Markdown mtimes and reloads
    automatically, which allows an operator to add a custom approved document in Pterodactyl without
    rebuilding the container. Search results are excerpts and provenance, not a generated answer.
    """

    def __init__(
        self,
        root: Path,
        *,
        require_approved: bool = True,
        default_top_k: int = 4,
        max_context_chars: int = 5200,
        min_score: float = 0.8,
    ) -> None:
        self.root = Path(root)
        self.require_approved = require_approved
        self.default_top_k = max(1, min(default_top_k, 8))
        self.max_context_chars = max(800, min(max_context_chars, 12000))
        self.min_score = max(0.0, min_score)
        self._lock = threading.RLock()
        self._signature: tuple[tuple[str, int, int], ...] = ()
        self._documents: tuple[KnowledgeDocument, ...] = ()
        self._errors: tuple[str, ...] = ()
        self.root.mkdir(parents=True, exist_ok=True)
        self.reload(force=True)

    def _files(self) -> list[Path]:
        return sorted(path for path in self.root.rglob("*.md") if path.is_file())

    def _current_signature(self) -> tuple[tuple[str, int, int], ...]:
        values: list[tuple[str, int, int]] = []
        for path in self._files():
            try:
                stat = path.stat()
            except OSError:
                continue
            values.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        return tuple(values)

    def _load_document(self, path: Path) -> KnowledgeDocument:
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        if not match:
            raise ValueError("missing YAML front matter")
        raw_meta = yaml.safe_load(match.group(1)) or {}
        if not isinstance(raw_meta, dict):
            raise ValueError("front matter must be a mapping")
        body = match.group(2).strip()
        title = str(raw_meta.get("title") or path.stem.replace("-", " ").title()).strip()
        category = str(raw_meta.get("category") or "general").strip().lower()
        tags = _string_list(raw_meta.get("tags"))
        approved = _bool(raw_meta.get("approved"), False)
        source_url = str(raw_meta.get("source_url") or "").strip()
        reviewed_at = str(raw_meta.get("reviewed_at") or "").strip()
        summary = str(raw_meta.get("summary") or "").strip()
        stat = path.stat()
        return KnowledgeDocument(
            path=path,
            title=title,
            category=category,
            tags=tags,
            approved=approved,
            source_url=source_url,
            reviewed_at=reviewed_at,
            summary=summary,
            body=body,
            token_counts=Counter(_tokens(f"{summary}\n{body}")),
            title_counts=Counter(_tokens(title)),
            tag_counts=Counter(_tokens(" ".join((category, *tags)))),
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
        )

    def reload(self, *, force: bool = False) -> bool:
        signature = self._current_signature()
        with self._lock:
            if not force and signature == self._signature:
                return False
            documents: list[KnowledgeDocument] = []
            errors: list[str] = []
            for path in self._files():
                try:
                    document = self._load_document(path)
                except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                    errors.append(f"{path}: {exc}")
                    continue
                if self.require_approved and not document.approved:
                    continue
                documents.append(document)
            self._documents = tuple(documents)
            self._errors = tuple(errors)
            self._signature = signature
            return True

    def status(self) -> dict[str, Any]:
        self.reload()
        with self._lock:
            by_category = Counter(document.category for document in self._documents)
            return {
                "ok": bool(self._documents) and not self._errors,
                "root": str(self.root),
                "approved_documents": len(self._documents),
                "categories": dict(sorted(by_category.items())),
                "errors": list(self._errors),
                "require_approved": self.require_approved,
            }

    @staticmethod
    def _sections(document: KnowledgeDocument) -> list[str]:
        blocks = [block.strip() for block in re.split(r"\n\s*\n", document.body) if block.strip()]
        if document.summary:
            blocks.insert(0, document.summary)
        return blocks or [document.body]

    @staticmethod
    def _best_excerpt(document: KnowledgeDocument, query_tokens: set[str], limit: int = 1300) -> str:
        scored: list[tuple[int, int, str]] = []
        for index, block in enumerate(KnowledgeBase._sections(document)):
            clean = re.sub(r"^#{1,6}\s*", "", block, flags=re.MULTILINE).strip()
            block_tokens = set(_tokens(clean))
            overlap = len(block_tokens & query_tokens)
            scored.append((overlap, -index, clean))
        scored.sort(reverse=True)
        chosen: list[str] = []
        total = 0
        for overlap, _, block in scored:
            if chosen and overlap == 0:
                continue
            remaining = limit - total
            if remaining <= 0:
                break
            clipped = block[:remaining].rstrip()
            if clipped:
                chosen.append(clipped)
                total += len(clipped) + 2
            if len(chosen) >= 3:
                break
        excerpt = "\n\n".join(chosen).strip()
        if len(excerpt) >= limit:
            excerpt = excerpt[: limit - 1].rstrip() + "…"
        return excerpt

    def search(
        self,
        question: str,
        *,
        category: str = "",
        top_k: int | None = None,
    ) -> dict[str, Any]:
        self.reload()
        query = str(question or "").strip()
        if not query:
            return {
                "ok": False,
                "found": False,
                "question": "",
                "error": "question_required",
                "safe_message": "I need the customer's question before I can search Floodman's approved information.",
                "results": [],
            }
        query_counts = _expanded_query_tokens(query)
        query_set = set(query_counts)
        requested_category = str(category or "").strip().lower()
        with self._lock:
            documents = [
                document
                for document in self._documents
                if not requested_category or document.category == requested_category
            ]
            errors = list(self._errors)
        if not documents:
            return {
                "ok": not errors,
                "found": False,
                "question": query,
                "category": requested_category,
                "results": [],
                "errors": errors,
                "safe_message": "I do not have an approved Floodman answer for that yet. I can take a message or connect you with the team.",
            }

        doc_frequency = Counter()
        for token in query_set:
            doc_frequency[token] = sum(
                1
                for document in documents
                if token in document.token_counts
                or token in document.title_counts
                or token in document.tag_counts
            )

        ranked: list[tuple[float, KnowledgeDocument]] = []
        total_docs = len(documents)
        normalized_query = _normal(query)
        for document in documents:
            score = 0.0
            for token, query_weight in query_counts.items():
                title_tf = document.title_counts.get(token, 0)
                tag_tf = document.tag_counts.get(token, 0)
                body_tf = document.token_counts.get(token, 0)
                if not (title_tf or tag_tf or body_tf):
                    continue
                idf = math.log((total_docs + 1) / (doc_frequency[token] + 1)) + 1.0
                body_component = (1.0 + math.log(body_tf)) * 1.15 if body_tf else 0.0
                score += query_weight * idf * (
                    min(title_tf, 2) * 4.0
                    + min(tag_tf, 2) * 3.0
                    + body_component
                )
            if normalized_query and normalized_query in _normal(
                f"{document.title} {' '.join(document.tags)} {document.body}"
            ):
                score += 8.0
            if requested_category and document.category == requested_category:
                score += 2.0
            if score >= self.min_score:
                ranked.append((score, document))
        ranked.sort(key=lambda item: (-item[0], item[1].title.lower()))

        limit = max(1, min(top_k or self.default_top_k, 8))
        results: list[dict[str, Any]] = []
        context_parts: list[str] = []
        remaining = self.max_context_chars
        for score, document in ranked[:limit]:
            excerpt = self._best_excerpt(document, query_set, limit=min(1400, remaining))
            if not excerpt:
                continue
            source = {
                "title": document.title,
                "category": document.category,
                "tags": list(document.tags),
                "source_url": document.source_url,
                "reviewed_at": document.reviewed_at,
                "path": str(document.path.relative_to(self.root))
                if document.path.is_relative_to(self.root)
                else str(document.path),
            }
            results.append({"score": round(score, 3), "excerpt": excerpt, **source})
            block = f"[{document.title}]\n{excerpt}"
            if len(block) <= remaining:
                context_parts.append(block)
                remaining -= len(block) + 2
            if remaining < 250:
                break

        found = bool(results)
        return {
            "ok": not errors,
            "found": found,
            "question": query,
            "category": requested_category,
            "result_count": len(results),
            "results": results,
            "answer_context": "\n\n".join(context_parts),
            "errors": errors,
            "answer_policy": (
                "Use only the approved excerpts returned here. Do not infer a warranty, price, diagnosis, "
                "arrival time, service-area promise, or project method that is not stated."
            ),
            "safe_message": (
                "I found approved Floodman information for that question."
                if found
                else "I do not have an approved Floodman answer for that yet. I can take a message or connect you with the team."
            ),
        }


def _iter_area_cities(service_area: dict[str, Any]) -> Iterable[str]:
    yielded: set[str] = set()
    for value in service_area.get("cities", []):
        city = str(value).strip()
        if city and _normal(city) not in yielded:
            yielded.add(_normal(city))
            yield city
    regions = service_area.get("regions", {})
    if isinstance(regions, dict):
        for values in regions.values():
            if not isinstance(values, list):
                continue
            for value in values:
                city = str(value).strip()
                if city and _normal(city) not in yielded:
                    yielded.add(_normal(city))
                    yield city


def resolve_service_area(
    service_area: dict[str, Any],
    *,
    zip_code: str = "",
    city: str = "",
    address: str = "",
) -> dict[str, Any]:
    """Resolve only exact published cities/ZIPs; unknown addresses remain manual-review cases."""

    area = service_area if isinstance(service_area, dict) else {}
    allowed_zips = {re.sub(r"\D", "", str(value)) for value in area.get("zip_codes", [])}
    excluded_zips = {
        re.sub(r"\D", "", str(value)) for value in area.get("excluded_zip_codes", [])
    }
    requested_zip = re.sub(r"\D", "", str(zip_code or ""))
    requested_city = _normal(str(city or ""))
    raw_address = str(address or "").strip()
    normalized_address = _normal(raw_address)
    cities = list(_iter_area_cities(area))
    city_map = {_normal(value): value for value in cities}
    area_summary = {
        "description": str(area.get("description") or ""),
        "state": str(area.get("state") or "Michigan"),
        "published_city_count": len(cities),
        "zip_matrix_configured": bool(allowed_zips),
    }

    if requested_zip and requested_zip in excluded_zips:
        return {
            "ok": True,
            "eligible": False,
            "matched_by": "excluded_zip",
            "matched_value": requested_zip,
            "requires_manual_confirmation": False,
            "service_area": area_summary,
        }
    if requested_zip and allowed_zips:
        eligible = requested_zip in allowed_zips
        return {
            "ok": True,
            "eligible": eligible,
            "matched_by": "zip" if eligible else "zip_not_listed",
            "matched_value": requested_zip,
            "requires_manual_confirmation": not eligible
            and _bool(area.get("require_manual_confirmation_when_unknown"), True),
            "service_area": area_summary,
        }
    if requested_city and requested_city in city_map:
        return {
            "ok": True,
            "eligible": True,
            "matched_by": "city",
            "matched_value": city_map[requested_city],
            "requires_manual_confirmation": False,
            "service_area": area_summary,
        }
    if normalized_address:
        raw_segments = [segment.strip() for segment in re.split(r"[,;\n]", raw_address) if segment.strip()]
        if len(raw_segments) > 1:
            candidate_segments = [_normal(segment) for segment in raw_segments[1:]]
        elif raw_segments and not re.match(r"^\s*\d", raw_segments[0]):
            candidate_segments = [_normal(raw_segments[0])]
        else:
            candidate_segments = []
        matches = [
            (len(normalized_city), display)
            for normalized_city, display in city_map.items()
            if any(
                segment == normalized_city
                or segment.startswith(normalized_city + " ")
                or segment.endswith(" " + normalized_city)
                for segment in candidate_segments
            )
        ]
        if matches:
            _, display = max(matches)
            return {
                "ok": True,
                "eligible": True,
                "matched_by": "address_city",
                "matched_value": display,
                "requires_manual_confirmation": False,
                "service_area": area_summary,
            }

    return {
        "ok": True,
        "eligible": None,
        "matched_by": "none",
        "matched_value": "",
        "requires_manual_confirmation": _bool(
            area.get("require_manual_confirmation_when_unknown"), True
        ),
        "service_area": area_summary,
    }
