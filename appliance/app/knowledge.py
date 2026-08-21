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
        return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 2}

    def reload(self) -> None:
        documents: list[KnowledgeDocument] = []
        for path in sorted(self.root.rglob("*.md")):
            text = path.read_text(encoding="utf-8")
            metadata: dict[str, Any] = {}
            body = text
            if text.startswith("---\n") and "\n---\n" in text[4:]:
                front, body = text[4:].split("\n---\n", 1)
                metadata = yaml.safe_load(front) or {}
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
