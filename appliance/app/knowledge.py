from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class KnowledgeDocument:
    title: str
    category: str
    tags: tuple[str, ...]
    body: str
    path: Path


class KnowledgeBase:
    def __init__(self, root: Path):
        self.root = root
        self.documents: list[KnowledgeDocument] = []
        self.reload()

    @staticmethod
    def _tokens(value: str) -> set[str]:
        stop = {"the", "and", "for", "you", "your", "our", "that", "this", "what", "when", "where", "who", "why", "how", "does", "with", "have", "are", "can", "could", "would", "will", "about", "there", "they", "their", "please", "tell", "more", "from", "floodman"}
        aliases = {"leaking": "leak", "leaks": "leak", "basements": "basement", "foundations": "foundation", "businesses": "commercial", "business": "commercial", "long": "years", "mould": "mold", "costs": "pricing", "cost": "pricing", "prices": "pricing", "price": "pricing", "estimates": "inspection", "estimate": "inspection"}
        return {aliases.get(token, token) for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 2 and token not in stop}

    @staticmethod
    def _parse(path: Path) -> tuple[dict[str, Any], str]:
        text = path.read_text(encoding="utf-8")
        metadata: dict[str, Any] = {}
        body = text
        if text.startswith("---\n") and "\n---\n" in text[4:]:
            front, body = text[4:].split("\n---\n", 1)
            metadata = yaml.safe_load(front) or {}
        return metadata, body.strip()

    def _managed_path(self, slug: str) -> Path:
        value = str(slug or "").strip().lower().removesuffix(".md")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,79}", value):
            raise ValueError("Document name must use 2-80 lowercase letters, numbers, dashes, or underscores.")
        path = (self.root / f"{value}.md").resolve()
        if path.parent != self.root.resolve():
            raise ValueError("Invalid knowledge document path.")
        return path

    def reload(self) -> None:
        documents: list[KnowledgeDocument] = []
        for path in sorted(self.root.rglob("*.md")):
            metadata, body = self._parse(path)
            if metadata.get("approved") is not True:
                continue
            documents.append(
                KnowledgeDocument(
                    title=str(metadata.get("title") or path.stem),
                    category=str(metadata.get("category") or "general"),
                    tags=tuple(str(v) for v in metadata.get("tags") or ()),
                    body=body.strip(),
                    path=path,
                )
            )
        self.documents = documents

    def managed_documents(self) -> list[dict[str, Any]]:
        output = []
        for path in sorted(self.root.glob("*.md")):
            metadata, body = self._parse(path)
            output.append(
                {
                    "slug": path.stem,
                    "title": str(metadata.get("title") or path.stem),
                    "category": str(metadata.get("category") or "general"),
                    "tags": ", ".join(str(value) for value in metadata.get("tags") or ()),
                    "approved": metadata.get("approved") is True,
                    "body": body,
                    "updated_at": path.stat().st_mtime,
                }
            )
        return output

    def managed_document(self, slug: str) -> dict[str, Any] | None:
        path = self._managed_path(slug)
        if not path.exists():
            return None
        metadata, body = self._parse(path)
        return {
            "slug": path.stem,
            "title": str(metadata.get("title") or path.stem),
            "category": str(metadata.get("category") or "general"),
            "tags": ", ".join(str(value) for value in metadata.get("tags") or ()),
            "approved": metadata.get("approved") is True,
            "body": body,
        }

    def save_document(
        self,
        *,
        slug: str,
        title: str,
        category: str,
        tags: str,
        body: str,
        approved: bool,
        original_slug: str = "",
    ) -> str:
        path = self._managed_path(slug)
        original = self._managed_path(original_slug) if original_slug else path
        if original != path and path.exists():
            raise ValueError("A document with that name already exists.")
        metadata = {
            "title": str(title or "").strip()[:160] or path.stem,
            "category": str(category or "general").strip()[:80] or "general",
            "approved": bool(approved),
            "tags": [value.strip()[:80] for value in str(tags or "").split(",") if value.strip()][:30],
        }
        content = "---\n" + yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True).strip() + "\n---\n\n" + str(body or "").strip() + "\n"
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        if original != path and original.exists():
            original.unlink()
        self.reload()
        return path.stem

    def delete_document(self, slug: str) -> None:
        path = self._managed_path(slug)
        if not path.exists():
            raise LookupError("Knowledge document not found.")
        path.unlink()
        self.reload()

    def search(self, query: str, top_k: int = 4, max_chars: int = 5200) -> list[KnowledgeDocument]:
        terms = self._tokens(query)
        if not terms:
            return []
        scored: list[tuple[float, KnowledgeDocument]] = []
        for document in self.documents:
            title = self._tokens(document.title + " " + " ".join(document.tags))
            body = self._tokens(document.body)
            overlap = len(terms & body)
            title_overlap = len(terms & title)
            score = title_overlap * 3.0 + overlap + (overlap / max(1.0, math.sqrt(len(body))))
            if score > 0:
                scored.append((score, document))
        result: list[KnowledgeDocument] = []
        used = 0
        for _, document in sorted(scored, key=lambda item: item[0], reverse=True):
            if len(result) >= top_k:
                break
            size = len(document.body)
            if result and used + size > max_chars:
                continue
            result.append(document)
            used += size
        return result

    def context(self, query: str) -> str:
        chunks = []
        for document in self.search(query):
            chunks.append(f"# {document.title}\n{document.body}")
        return "\n\n".join(chunks)

    def answer_context(self, query: str, documents: list[KnowledgeDocument] | None = None, *, intent_query: str | None = None) -> str:
        """Keep only a few relevant approved sentences for the CPU voice budget."""
        terms = self._tokens(query)
        intent = intent_query or query
        procedural = bool(re.search(r"\b(?:how|involve|process|explain|happens)\b", intent, re.I)) and not bool(re.search(r"\b(?:how long|how many years|in business|been around|experience)\b", intent, re.I))
        compare = bool(re.search(r"\b(?:compare|difference|versus|vs)\b", query, re.I))
        chunks, used = [], 0
        for document in (documents if documents is not None else self.search(query, top_k=2))[:2 if compare else 1]:
            sentences = [re.sub(r"\s+", " ", part).strip() for part in re.split(r"(?<=[.!?])\s+|\n\s*\n", document.body)]
            def relevance(item):
                sentence = item[1]
                process_fact = procedural and re.search(r"\b(?:projects may|may involve|may include|may consider|visit helps|inspection may)\b", sentence, re.I)
                return len(terms & self._tokens(sentence)) + (5 if process_fact else 0)
            scored = sorted(enumerate(sentences), key=relevance, reverse=True)
            selected = []
            for index, sentence in scored:
                if not sentence or sentence.startswith('#') or used + len(sentence) > 1200:
                    continue
                selected.append((index, sentence))
                used += len(sentence)
                if len(selected) == 2:
                    break
            if selected:
                chunks.append(document.title + ": " + " ".join(sentence for _, sentence in sorted(selected)))
        return "\n".join(chunks)

    def direct_answer(self, query: str) -> str:
        """Immediate company facts, enabled only by an approved source sentence."""
        words = " ".join(re.findall(r"[a-z0-9]+", query.lower()))
        for document in self.documents:
            if document.category != "company":
                continue
            body = re.sub(r"\s+", " ", document.body).lower()
            if re.search(r"\b(?:in business|been around|how many years|how long (?:have|has)|experience)\b", words):
                tenure = re.search(r"\b(?:more than|over) (?:[a-z]+|\d+) years\b", body)
                if tenure and "northern michigan" in body:
                    return f"Floodman has been serving Northern Michigan homeowners for {tenure[0]}."
            scope_question = re.search(r"\b(?:do you (?:help|work|serve|handle)|you (?:guys )?(?:help|work|serve|handle)|work with|what about)\b", words)
            if scope_question and re.search(r"\b(?:commercial|business(?:es)?|rental|residential)\b", words):
                if "homeowners and commercial property owners" in body:
                    return "Yes, we help both homeowners and commercial property owners. We can take the details and get them to the right team."
        return ""

    def conversation_answer(self, query: str, *, intent_query: str, documents: list[KnowledgeDocument]) -> str:
        """Immediate common service answers, conditional on current approved facts.

        Keep CPU generation for questions that need composition. These answers
        describe a service; they never diagnose a property or promise a result.
        """
        words = " ".join(re.findall(r"[a-z0-9]+", intent_query.lower()))
        if re.search(r"\b(?:always|definitely|guarantee|guaranteed|forever|fix|cure|prevent|cost|price|insurance|warranty|how long)\b", words):
            return ""
        if not documents or documents[0].category != "crawl_space":
            return ""
        document = documents[0]
        body = re.sub(r"\s+", " ", document.body).lower()
        if not self._tokens(query) & self._tokens(document.title + " " + " ".join(document.tags)):
            return ""
        process = re.search(r"\b(?:how does .+ work|what does .+ involve|what happens|explain .+ process|tell me .+ process)\b", words)
        if process and all(fact in body for fact in ("projects may involve a vapor barrier, sealing, drainage, dehumidification", "require inspection")):
            return "Depending on your space, the work may include a vapor barrier, sealing, drainage, or dehumidification to manage moisture."
        help_question = re.search(r"\b(?:does|would|could|can) .+ help\b", words)
        concerns = re.search(r"\b(?:moisture|humidity|musty|smell|smells|odor|odors)\b", words)
        if help_question and concerns and all(fact in body for fact in ("encapsulation and moisture control", "musty odor, humidity")):
            return "We help with crawl-space moisture and concerns like musty odors. An inspection helps the team understand what's causing it."
        return ""
