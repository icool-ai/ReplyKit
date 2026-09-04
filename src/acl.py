"""Document-level FAQ ACL for private retrieval.

Visibility:
  - public: any authenticated user may retrieve
  - private: only owner_username (or ops) may retrieve

Ops role sees all FAQs when ACL is enabled.
When FAQ_ACL_ENABLED is false, every FAQ is treated as visible (legacy).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from qdrant_client.http.models import (
    FieldCondition,
    Filter,
    MatchValue,
)

Visibility = Literal["public", "private"]
VISIBILITY_PUBLIC: Visibility = "public"
VISIBILITY_PRIVATE: Visibility = "private"
VALID_VISIBILITIES = frozenset({VISIBILITY_PUBLIC, VISIBILITY_PRIVATE})


@dataclass(frozen=True)
class RetrievalPrincipal:
    """Who is asking — used to build retrieval filters."""

    username: str = ""
    role: str = "user"

    @property
    def is_ops(self) -> bool:
        return (self.role or "").strip().lower() == "ops"

    @property
    def normalized_username(self) -> str:
        return (self.username or "").strip()


def normalize_visibility(raw: Any) -> Visibility:
    value = str(raw or VISIBILITY_PUBLIC).strip().lower()
    if value in VALID_VISIBILITIES:
        return value  # type: ignore[return-value]
    return VISIBILITY_PUBLIC


def normalize_allow_egress(raw: Any, *, default: bool = True) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"0", "false", "no", "off"}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return default


def is_faq_visible(
    principal: RetrievalPrincipal,
    *,
    owner_username: str,
    visibility: str,
    acl_enabled: bool,
) -> bool:
    if not acl_enabled:
        return True
    if principal.is_ops:
        return True
    vis = normalize_visibility(visibility)
    if vis == VISIBILITY_PUBLIC:
        return True
    owner = (owner_username or "").strip()
    return bool(owner) and owner == principal.normalized_username


def filter_visible_faq_ids(
    rows: list[Any],
    principal: RetrievalPrincipal,
    *,
    acl_enabled: bool,
) -> set[str] | None:
    """Return allowed faq_id set, or None when no id filter is needed (all / ops)."""
    if not acl_enabled or principal.is_ops:
        return None
    allowed: set[str] = set()
    for row in rows:
        faq_id = str(getattr(row, "id", "") or "").strip()
        if not faq_id:
            continue
        if is_faq_visible(
            principal,
            owner_username=str(getattr(row, "owner_username", "") or ""),
            visibility=str(getattr(row, "visibility", VISIBILITY_PUBLIC) or ""),
            acl_enabled=True,
        ):
            allowed.add(faq_id)
    return allowed


def build_faq_acl_filter(
    principal: RetrievalPrincipal,
    *,
    acl_enabled: bool,
    doc_types: list[str] | None = None,
) -> Filter | None:
    """Qdrant filter: doc_type(+optional) AND visibility ACL.

    Missing legacy payload fields are treated as public via an extra should branch
    that matches empty owner with public (new upserts always set visibility).
    """
    must: list[Any] = []
    if doc_types:
        if len(doc_types) == 1:
            must.append(
                FieldCondition(key="doc_type", match=MatchValue(value=doc_types[0]))
            )
        else:
            must.append(
                Filter(
                    should=[
                        FieldCondition(key="doc_type", match=MatchValue(value=t))
                        for t in doc_types
                    ]
                )
            )

    if acl_enabled and not principal.is_ops:
        username = principal.normalized_username
        acl_should: list[Any] = [
            FieldCondition(
                key="visibility", match=MatchValue(value=VISIBILITY_PUBLIC)
            ),
        ]
        if username:
            acl_should.append(
                Filter(
                    must=[
                        FieldCondition(
                            key="visibility",
                            match=MatchValue(value=VISIBILITY_PRIVATE),
                        ),
                        FieldCondition(
                            key="owner_username",
                            match=MatchValue(value=username),
                        ),
                    ]
                )
            )
        must.append(Filter(should=acl_should))

    if not must:
        return None
    if len(must) == 1 and isinstance(must[0], Filter):
        return must[0]
    return Filter(must=must)


def meta_allow_egress(metadata: dict[str, Any] | None, *, default: bool = True) -> bool:
    if not metadata:
        return default
    return normalize_allow_egress(metadata.get("allow_egress"), default=default)


def filter_docs_for_egress(
    docs: list[Any],
    *,
    default_allow: bool = True,
) -> list[Any]:
    """Keep only chunks allowed to leave the private boundary (rerank / LLM)."""
    out: list[Any] = []
    for doc in docs:
        meta = getattr(doc, "metadata", None) or {}
        if meta_allow_egress(meta, default=default_allow):
            out.append(doc)
    return out
