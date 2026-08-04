"""Dify external knowledge API helpers (POST /retrieval).

Protocol: https://docs.dify.ai/zh/cloud/use-dify/knowledge/external-knowledge-api
Response is Dify-native JSON (not the project {code,message,data} envelope).
"""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document


def dify_error(*, error_code: int, error_msg: str) -> dict[str, Any]:
    return {"error_code": error_code, "error_msg": error_msg}


def docs_to_records(
    docs: list[Document],
    *,
    score_threshold: float,
    top_k: int,
) -> list[dict[str, Any]]:
    """Map FAQ Documents to Dify ``records``; filter by score and cap at top_k."""
    records: list[dict[str, Any]] = []
    for doc in docs:
        meta = dict(doc.metadata or {})
        try:
            score = float(meta.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score_threshold > 0 and score < score_threshold:
            continue

        question = str(meta.get("question") or "").strip()
        answer = str(meta.get("answer") or "").strip()
        if question and answer:
            content = f"标准问：{question}\n答案：{answer}"
        elif answer:
            content = answer
        else:
            content = str(doc.page_content or "").strip()
        if not content:
            continue

        title = question or content[:80]
        out_meta: dict[str, Any] = {}
        faq_id = str(meta.get("faq_id") or "").strip()
        if faq_id:
            out_meta["faq_id"] = faq_id
        category = str(meta.get("category") or "").strip()
        if category:
            out_meta["category"] = category
        source = str(meta.get("source") or "").strip()
        if source:
            out_meta["source"] = source

        records.append(
            {
                "content": content,
                "score": score,
                "title": title,
                "metadata": out_meta,
            }
        )
        if len(records) >= top_k:
            break
    return records
