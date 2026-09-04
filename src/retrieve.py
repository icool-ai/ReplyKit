"""Hybrid FAQ retrieval: vector + BM25 (RRF) then optional DashScope rerank."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from langchain_core.documents import Document

from src.acl import (
    RetrievalPrincipal,
    is_faq_visible,
    meta_allow_egress,
)
from src.config import Settings
from src.faq_store import FaqStore
from src.model_gateway import get_model_gateway

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]", re.IGNORECASE)
_RRF_K = 60
# 口语问法里的虚词/套话，不做词面佐证（避免「如何检查」≈「如何查看」）
_LEX_STOP = frozenset(
    {
        "如何",
        "怎么",
        "怎样",
        "怎么整",
        "什么",
        "一下",
        "请问",
        "可以",
        "能否",
        "是否",
        "给我",
        "说明",
        "介绍",
        "帮忙",
        "帮我",
        "我想",
        "我要",
        "一下",
        "一下下",
    }
)


def tokenize(text: str) -> list[str]:
    """Lightweight Chinese/English tokens: latin words + char unigrams/bigrams."""
    raw = (text or "").strip().lower()
    if not raw:
        return []
    parts = _TOKEN_RE.findall(raw)
    tokens: list[str] = []
    chars: list[str] = []
    for p in parts:
        if "\u4e00" <= p <= "\u9fff":
            chars.append(p)
            tokens.append(p)
        else:
            if chars:
                tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
                chars = []
            tokens.append(p)
    if chars:
        tokens.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
    return tokens


@dataclass
class PhraseDoc:
    faq_id: str
    question: str
    answer: str
    match_text: str
    phrase_role: str
    category: str = ""
    owner_username: str = ""
    visibility: str = "public"
    allow_egress: bool = True
    tokens: list[str] = field(default_factory=list)


class Bm25Index:
    """In-memory BM25 over FAQ phrases (standard + similar)."""

    def __init__(self, docs: list[PhraseDoc]) -> None:
        self.docs = docs
        self._avgdl = 1.0
        self._df: dict[str, int] = {}
        self._tf: list[dict[str, int]] = []
        self._N = len(docs)
        self._build()

    def _build(self) -> None:
        if not self.docs:
            return
        total_len = 0
        for doc in self.docs:
            tf: dict[str, int] = {}
            for t in doc.tokens:
                tf[t] = tf.get(t, 0) + 1
            self._tf.append(tf)
            total_len += max(len(doc.tokens), 1)
            for t in tf:
                self._df[t] = self._df.get(t, 0) + 1
        self._avgdl = total_len / max(self._N, 1)

    def search(
        self,
        query: str,
        k: int = 20,
        *,
        principal: RetrievalPrincipal | None = None,
        acl_enabled: bool = False,
    ) -> list[tuple[PhraseDoc, float]]:
        q_tokens = tokenize(query)
        if not q_tokens or not self.docs:
            return []
        who = principal or RetrievalPrincipal()
        scores: list[tuple[int, float]] = []
        for i, tf in enumerate(self._tf):
            doc = self.docs[i]
            if not is_faq_visible(
                who,
                owner_username=doc.owner_username,
                visibility=doc.visibility,
                acl_enabled=acl_enabled,
            ):
                continue
            score = 0.0
            dl = max(sum(tf.values()), 1)
            for t in q_tokens:
                if t not in tf:
                    continue
                df = self._df.get(t, 0)
                idf = math.log(1 + (self._N - df + 0.5) / (df + 0.5))
                freq = tf[t]
                # BM25 with k1=1.5, b=0.75
                denom = freq + 1.5 * (1 - 0.75 + 0.75 * dl / self._avgdl)
                score += idf * (freq * 2.5) / denom
            if score > 0:
                scores.append((i, score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return [(self.docs[i], s) for i, s in scores[:k]]


_bm25_lock = Lock()
_bm25_cache: tuple[str, Bm25Index] | None = None


def _faq_fingerprint(store: FaqStore) -> str:
    rows = store.list_enabled()
    parts = [
        f"{r.id}:{r.updated_at}:{len(r.similar)}:{r.visibility}:{r.owner_username}:{int(r.allow_egress)}"
        for r in rows
    ]
    return f"{len(rows)}|" + "|".join(parts[:50]) + f"|tail:{parts[-1] if parts else ''}"


def get_bm25_index(store: FaqStore) -> Bm25Index:
    global _bm25_cache
    fp = _faq_fingerprint(store)
    with _bm25_lock:
        if _bm25_cache and _bm25_cache[0] == fp:
            return _bm25_cache[1]
        docs: list[PhraseDoc] = []
        for row in store.list_enabled():
            phrases = [("question", row.question), *[("similar", s) for s in row.similar]]
            seen: set[str] = set()
            for role, text in phrases:
                key = text.strip().lower()
                if not key or key in seen:
                    continue
                seen.add(key)
                docs.append(
                    PhraseDoc(
                        faq_id=row.id,
                        question=row.question,
                        answer=row.answer,
                        match_text=text.strip(),
                        phrase_role=role,
                        category=row.category,
                        owner_username=row.owner_username,
                        visibility=row.visibility,
                        allow_egress=row.allow_egress,
                        tokens=tokenize(text),
                    )
                )
        index = Bm25Index(docs)
        _bm25_cache = (fp, index)
        return index


def invalidate_bm25_cache() -> None:
    global _bm25_cache
    with _bm25_lock:
        _bm25_cache = None


def rrf_fuse(
    ranked_lists: list[list[str]],
    *,
    k: int = _RRF_K,
) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion over lists of keys (e.g. faq_id|match_text)."""
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, key in enumerate(ranked, start=1):
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def phrase_to_document(phrase: PhraseDoc, *, score: float) -> Document:
    return Document(
        page_content=phrase.match_text,
        metadata={
            "source": "faq_db",
            "doc_type": "faq",
            "faq_id": phrase.faq_id,
            "category": phrase.category,
            "question": phrase.question,
            "answer": phrase.answer,
            "phrase_role": phrase.phrase_role,
            "match_text": phrase.match_text,
            "embed_mode": "per_phrase",
            "owner_username": phrase.owner_username,
            "visibility": phrase.visibility,
            "allow_egress": phrase.allow_egress,
            "images": [],
            "score": score,
        },
    )


def dashscope_rerank(
    *,
    api_key: str,
    model: str,
    query: str,
    documents: list[str],
    top_n: int,
    timeout: float = 30.0,
    request_id: str = "",
    username: str = "",
) -> list[tuple[int, float]]:
    """Backward-compatible wrapper → model gateway rerank."""
    del api_key  # key is owned by the gateway
    return get_model_gateway().rerank(
        query=query,
        documents=documents,
        model=model,
        top_n=top_n,
        timeout=timeout,
        request_id=request_id,
        username=username,
    )


def _has_lexical_support(query: str, doc: Document) -> bool:
    """Content-word overlap between query and FAQ phrase — soft-band corroboration."""
    meaningful = {
        t
        for t in tokenize(query)
        if len(t) >= 2 and t not in _LEX_STOP
    }
    if not meaningful:
        return False
    meta = doc.metadata or {}
    text = f"{meta.get('match_text') or ''} {meta.get('question') or ''}"
    return bool(meaningful & set(tokenize(text)))


def _adaptive_rerank_cut(
    top_score: float,
    *,
    floor: float,
    relative: float,
) -> float:
    """Relative cut against this round's best score; never below noise floor."""
    if top_score <= 0:
        return 1.0
    return max(floor, top_score * relative)


def hybrid_faq_retrieve(
    settings: Settings,
    query: str,
    *,
    vector_docs: list[Document],
    vector_candidates: list[tuple[float, str, str]],
    faq_store: FaqStore,
    principal: RetrievalPrincipal | None = None,
) -> tuple[list[Document], list[tuple[float, str, str]], dict[str, Any]]:
    """Vector + BM25 → RRF → optional rerank.

    Returns accepted docs (already gated), display candidates, debug meta.
    """
    limit = settings.top_k
    clarify = settings.clarify_threshold
    vector_k = settings.hybrid_vector_k
    bm25_k = settings.hybrid_bm25_k
    rerank_n = settings.rerank_top_n
    who = principal or RetrievalPrincipal()

    # Index vector hits by fusion key
    by_key: dict[str, Document] = {}
    vector_keys: list[str] = []
    for doc in vector_docs:
        meta = doc.metadata or {}
        faq_id = str(meta.get("faq_id") or "")
        match = str(meta.get("match_text") or doc.page_content or "").strip()
        key = f"{faq_id}::{match}"
        if key not in by_key:
            by_key[key] = doc
            vector_keys.append(key)
        meta["vector_score"] = float(meta.get("score") or 0.0)

    # Also keep lower-scoring vector candidates that didn't pass clarify yet
    # so BM25/rerank can still promote/demote — rebuild from raw candidates via store.
    bm25 = get_bm25_index(faq_store)
    bm25_hits = bm25.search(
        query,
        k=bm25_k,
        principal=who,
        acl_enabled=settings.faq_acl_enabled,
    )
    bm25_keys: list[str] = []
    bm25_score_map: dict[str, float] = {}
    for phrase, bscore in bm25_hits:
        key = f"{phrase.faq_id}::{phrase.match_text}"
        bm25_keys.append(key)
        bm25_score_map[key] = bscore
        if key not in by_key:
            by_key[key] = phrase_to_document(phrase, score=0.0)

    fused = rrf_fuse([vector_keys[:vector_k], bm25_keys[:bm25_k]])
    pool_keys = [k for k, _ in fused[: max(rerank_n, limit * 3)]]
    pool_docs = [by_key[k] for k in pool_keys if k in by_key]

    debug: dict[str, Any] = {
        "hybrid": True,
        "vector_hits": len(vector_keys),
        "bm25_hits": len(bm25_keys),
        "fused": len(fused),
        "rerank": False,
        "acl": settings.faq_acl_enabled,
        "principal": who.normalized_username or "",
    }

    ordered: list[Document] = list(pool_docs)
    if settings.rerank_enabled and pool_docs:
        egress_indices = [
            i
            for i, d in enumerate(pool_docs)
            if meta_allow_egress(d.metadata or {}, default=True)
        ]
        debug["egress_docs"] = len(egress_indices)
        debug["local_only_docs"] = len(pool_docs) - len(egress_indices)
        docs_text = [
            f"{(pool_docs[i].metadata or {}).get('match_text') or pool_docs[i].page_content}"
            for i in egress_indices
        ]
        try:
            if not egress_indices:
                debug["rerank_skipped"] = "no_egress_docs"
                ranked = []
            else:
                ranked = get_model_gateway(settings).rerank(
                    query=query,
                    documents=docs_text,
                    model=settings.rerank_model,
                    top_n=min(rerank_n, len(docs_text)),
                    username=who.normalized_username,
                )
            if ranked:
                debug["rerank"] = True
                debug["rerank_model"] = settings.rerank_model
                reordered: list[Document] = []
                seen: set[int] = set()
                for local_idx, rscore in ranked:
                    if local_idx < 0 or local_idx >= len(egress_indices):
                        continue
                    idx = egress_indices[local_idx]
                    if idx in seen:
                        continue
                    seen.add(idx)
                    doc = pool_docs[idx]
                    meta = dict(doc.metadata or {})
                    meta["rerank_score"] = rscore
                    meta["score"] = float(rscore)
                    reordered.append(
                        Document(page_content=doc.page_content, metadata=meta)
                    )
                # 精排成功后：未返回分数的候选一律视为丢弃，避免向量噪声绕过精排
                egress_set = set(egress_indices)
                for i, doc in enumerate(pool_docs):
                    if i in seen:
                        continue
                    meta = dict(doc.metadata or {})
                    if i in egress_set:
                        meta["rerank_score"] = 0.0
                        meta["score"] = 0.0
                        meta["_rerank_missing"] = True
                    else:
                        meta["_local_only"] = True
                        if "score" not in meta or meta.get("score") is None:
                            meta["score"] = float(meta.get("vector_score") or 0.0)
                    reordered.append(
                        Document(page_content=doc.page_content, metadata=meta)
                    )
                ordered = reordered
            elif not egress_indices:
                for doc in ordered:
                    meta = dict(doc.metadata or {})
                    meta["_local_only"] = True
                    doc.metadata = meta
        except Exception as exc:  # noqa: BLE001 — fall back to RRF order
            logger.warning("rerank failed, fallback to RRF: %s", exc)
            debug["rerank_error"] = str(exc)[:200]
            for i, key in enumerate(pool_keys):
                doc = by_key[key]
                meta = dict(doc.metadata or {})
                # normalize RRF into a soft score for display
                rrf = fused[i][1] if i < len(fused) else 0.0
                meta["rrf_score"] = rrf
                v = float(meta.get("vector_score") or 0.0)
                meta["score"] = v if v > 0 else min(0.99, rrf * 10)
                by_key[key] = Document(page_content=doc.page_content, metadata=meta)
            ordered = [by_key[k] for k in pool_keys if k in by_key]

    # Attach bm25 scores
    for doc in ordered:
        meta = doc.metadata or {}
        faq_id = str(meta.get("faq_id") or "")
        match = str(meta.get("match_text") or doc.page_content or "").strip()
        key = f"{faq_id}::{match}"
        if key in bm25_score_map:
            meta["bm25_score"] = bm25_score_map[key]

    # Adaptive rerank gate (fundamentally NOT a single absolute 0.25 cut):
    # - floor: absolute noise
    # - relative: keep docs near this round's top score
    # - soft band (top < high): require BM25 or lexical support (avoid 物流≈权限)
    accepted: list[Document] = []
    display: list[tuple[float, str, str]] = []
    floor = settings.rerank_min_score
    high = settings.rerank_high_score
    relative = settings.rerank_relative

    scored_rerank = [
        float((d.metadata or {}).get("rerank_score") or 0.0)
        for d in ordered
        if (d.metadata or {}).get("rerank_score") is not None
        and not (d.metadata or {}).get("_rerank_missing")
    ]
    top_r = max(scored_rerank) if scored_rerank else 0.0
    cut = _adaptive_rerank_cut(top_r, floor=floor, relative=relative)
    soft_band = top_r > 0 and top_r < high
    debug["rerank_top"] = round(top_r, 4)
    debug["rerank_cut"] = round(cut, 4)
    debug["rerank_soft_band"] = soft_band

    for doc in ordered:
        meta = dict(doc.metadata or {})
        label = str(meta.get("match_text") or meta.get("question") or "")
        vector_score = float(meta.get("vector_score") or 0.0)
        rerank_score = meta.get("rerank_score")
        bm25_score = float(meta.get("bm25_score") or 0.0)
        lexical = _has_lexical_support(query, doc)

        if rerank_score is not None:
            final = float(rerank_score)
            if meta.get("_rerank_missing"):
                display.append((final, f"{label} [未进精排]", "faq"))
                continue
            if final < floor:
                display.append((final, f"{label} [精排噪声]", "faq"))
                continue
            if final < cut:
                display.append((final, f"{label} [精排相对丢弃]", "faq"))
                continue
            # Soft paraphrase band: must have keyword/BM25 support
            if soft_band and final < high:
                if bm25_score <= 0 and not lexical:
                    display.append((final, f"{label} [精排无词面佐证]", "faq"))
                    continue
            meta["rerank_score"] = final
            meta["rerank_tier"] = "high" if final >= high or not soft_band else "soft"
            # 下游策略：软命中至少进入可采用区间，避免口语改写被当成「未检索到」
            if meta["rerank_tier"] == "soft":
                meta["score"] = max(vector_score, clarify)
            else:
                meta["score"] = vector_score if vector_score > 0 else final
            display_score = final
        else:
            final = float(meta.get("score") or vector_score)
            if final < clarify and bm25_score <= 0 and not lexical:
                display.append((final, label, "faq"))
                continue
            if vector_score < clarify and (bm25_score > 0 or lexical):
                meta["score"] = max(
                    clarify, min(settings.direct_threshold - 0.01, 0.55)
                )
                final = float(meta["score"])
            elif final < clarify:
                display.append((final, label, "faq"))
                continue
            display_score = float(meta.get("rerank_score") or final)

        display.append((display_score, label, "faq"))
        accepted.append(Document(page_content=doc.page_content, metadata=meta))
        if len(accepted) >= limit:
            break

    # If nothing accepted, still show top display from ordered for logs
    if not display and ordered:
        for doc in ordered[: max(limit * 2, 8)]:
            meta = doc.metadata or {}
            label = str(meta.get("match_text") or meta.get("question") or "")
            final = float(meta.get("score") or meta.get("vector_score") or 0.0)
            display.append((final, label, "faq"))

    # Prefer original vector candidate list for logging richness when hybrid empty
    if not display and vector_candidates:
        display = list(vector_candidates)

    logger.info(
        "hybrid retrieve: query=%r accepted=%s vector=%s bm25=%s fused=%s "
        "rerank=%s top=%.4f cut=%.4f soft=%s err=%s",
        query[:80],
        len(accepted),
        debug.get("vector_hits"),
        debug.get("bm25_hits"),
        debug.get("fused"),
        debug.get("rerank"),
        top_r,
        cut,
        soft_band,
        debug.get("rerank_error"),
    )
    return accepted, display, debug
