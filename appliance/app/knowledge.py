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
