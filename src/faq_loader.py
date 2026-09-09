"""Load structured FAQ entries from JSON or DB rows into Documents."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

# Cap similar questions per FAQ to control embedding cost / index size.
DEFAULT_MAX_SIMILAR = 30


def _normalize_phrases(
    question: str, similar: list[str], max_similar: int
) -> list[tuple[str, str]]:
    """Return unique (role, text) pairs: standard question first, then similars."""
    phrases: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(role: str, text: str) -> None:
        key = text.strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        phrases.append((role, text.strip()))

    add("question", question)
    for item in similar[:max_similar]:
        add("similar", item)
    return phrases


def faq_entry_to_documents(
    *,
    faq_id: str,
    question: str,
    answer: str,
    similar: list[str] | None = None,
    category: str = "",
    source: str = "faq_db",
    max_similar: int = DEFAULT_MAX_SIMILAR,
    owner_username: str = "",
    visibility: str = "public",
    allow_egress: bool = True,
) -> list[Document]:
    """Expand one FAQ into multiple vectors (standard + each similar)."""
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not question or not answer:
        return []

    similar_list = [str(s).strip() for s in (similar or []) if str(s).strip()]
    phrases = _normalize_phrases(question, similar_list, max_similar)
    documents: list[Document] = []
    for phrase_index, (role, text) in enumerate(phrases):
        documents.append(
            Document(
                page_content=text,
                metadata={
                    "source": source,
                    "doc_type": "faq",
                    "faq_id": faq_id,
                    "category": (category or "").strip(),
                    "question": question,
                    "answer": answer,
                    "phrase_role": role,
                    "match_text": text,
                    "phrase_index": phrase_index,
                    "embed_mode": "per_phrase",
                    "owner_username": (owner_username or "").strip(),
                    "visibility": (visibility or "public").strip().lower()
                    or "public",
                    "allow_egress": bool(allow_egress),
                    "images": [],
                },
            )
        )
    return documents


def load_faq_entries(
    entries: list[dict[str, Any]],
    *,
    source: str = "faq_json",
    max_similar: int = DEFAULT_MAX_SIMILAR,
) -> list[Document]:
    """Expand a list of FAQ dicts into Documents."""
    documents: list[Document] = []
    for index, item in enumerate(entries, 1):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question", "")).strip()
        answer = str(item.get("answer", "")).strip()
        if not question or not answer:
            continue
        similar = item.get("similar") or []
        if isinstance(similar, str):
            similar = [similar]
        faq_id = str(item.get("id") or f"faq_{index:03d}")
        category = str(item.get("category") or "").strip()
        documents.extend(
            faq_entry_to_documents(
                faq_id=faq_id,
                question=question,
                answer=answer,
                similar=[str(s) for s in similar],
                category=category,
                source=source,
                max_similar=max_similar,
            )
        )
    return documents


def load_faq_json(
    path: Path,
    *,
    max_similar: int = DEFAULT_MAX_SIMILAR,
) -> list[Document]:
    """Expand each FAQ in a JSON file into multiple vectors.

    Kept for import / legacy tooling; runtime indexing prefers the FAQ DB.
    """
    path = path.resolve()
    if not path.exists():
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"FAQ JSON 须为数组：{path}")

    return load_faq_entries(
        [item for item in raw if isinstance(item, dict)],
        source=str(path),
        max_similar=max_similar,
    )
