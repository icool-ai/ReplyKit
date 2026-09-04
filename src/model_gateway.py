"""Process-local model gateway: sole egress for public model APIs.

Business code must not hold DashScope keys for ad-hoc HTTP; call this module
for embed / rerank / chat. Knowledge indexes and ACL stay private.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from src.config import Settings
from src.egress import sanitize_outbound_text, sanitize_outbound_texts

logger = logging.getLogger(__name__)

Purpose = Literal[
    "embed_query",
    "embed_documents",
    "rerank",
    "chat",
    "intent",
    "context_rewrite",
]

_lock = threading.Lock()
_gateway: "ModelGateway | None" = None


@dataclass(frozen=True)
class GatewayCallContext:
    """Optional audit fields attached to one outbound model call."""

    request_id: str = ""
    username: str = ""
    purpose: Purpose = "chat"


class ModelGateway:
    """Owns API credentials and performs audited public-model calls."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._api_key = settings.dashscope_api_key
        self._api_base = settings.openai_api_base
        self._max_rerank_docs = max(1, int(settings.model_egress_max_rerank_docs))
        self._max_chunk_chars = max(64, int(settings.model_egress_max_chunk_chars))
        self._redact = bool(settings.model_egress_redact_pii)

    def _new_request_id(self, explicit: str = "") -> str:
        return (explicit or "").strip() or uuid.uuid4().hex[:16]

    def _audit(
        self,
        *,
        purpose: Purpose,
        request_id: str,
        username: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "event": "model_egress",
            "purpose": purpose,
            "request_id": request_id,
            "username": username or "",
            "ts": int(time.time()),
        }
        if extra:
            payload.update(extra)
        # Never log full prompts / document bodies.
        logger.info("model_gateway %s", json.dumps(payload, ensure_ascii=False))

    def _maybe_redact(self, text: str) -> str:
        if not self._redact:
            if self._max_chunk_chars and len(text) > self._max_chunk_chars:
                return text[: self._max_chunk_chars]
            return text
        return sanitize_outbound_text(text, max_chars=self._max_chunk_chars)

    def embeddings(self) -> OpenAIEmbeddings:
        """LangChain embeddings client (key only via gateway)."""
        return OpenAIEmbeddings(
            model=self._settings.embedding_model,
            openai_api_key=self._api_key,
            openai_api_base=self._api_base,
            check_embedding_ctx_length=False,
        )

    def embed_query(
        self,
        text: str,
        *,
        request_id: str = "",
        username: str = "",
    ) -> list[float]:
        rid = self._new_request_id(request_id)
        outbound = self._maybe_redact(text)
        self._audit(
            purpose="embed_query",
            request_id=rid,
            username=username,
            extra={"chars": len(outbound)},
        )
        return self.embeddings().embed_query(outbound)

    def embed_documents(
        self,
        texts: list[str],
        *,
        request_id: str = "",
        username: str = "",
    ) -> list[list[float]]:
        rid = self._new_request_id(request_id)
        if self._redact:
            outbound = sanitize_outbound_texts(
                texts, max_chars=self._max_chunk_chars
            )
        else:
            outbound = [
                (t[: self._max_chunk_chars] if self._max_chunk_chars else t)
                for t in texts
            ]
        self._audit(
            purpose="embed_documents",
            request_id=rid,
            username=username,
            extra={"count": len(outbound)},
        )
        return self.embeddings().embed_documents(outbound)

    def chat_llm(
        self,
        *,
        purpose: Purpose = "chat",
        model: str | None = None,
        temperature: float | None = None,
        request_id: str = "",
        username: str = "",
    ) -> ChatOpenAI:
        rid = self._new_request_id(request_id)
        self._audit(
            purpose=purpose,
            request_id=rid,
            username=username,
            extra={"model": model or self._settings.chat_model},
        )
        return ChatOpenAI(
            model=model or self._settings.chat_model,
            openai_api_key=self._api_key,
            openai_api_base=self._api_base,
            temperature=float(
                self._settings.answer_temperature
                if temperature is None
                else temperature
            ),
        )

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        model: str | None = None,
        top_n: int = 8,
        request_id: str = "",
        username: str = "",
        timeout: float = 30.0,
    ) -> list[tuple[int, float]]:
        """DashScope text-rerank via gateway. documents must already be egress-safe."""
        if not documents:
            return []
        rid = self._new_request_id(request_id)
        capped = documents[: self._max_rerank_docs]
        if self._redact:
            outbound_docs = sanitize_outbound_texts(
                capped, max_chars=self._max_chunk_chars
            )
            outbound_query = sanitize_outbound_text(
                query, max_chars=self._max_chunk_chars
            )
        else:
            outbound_docs = [
                d[: self._max_chunk_chars] if self._max_chunk_chars else d
                for d in capped
            ]
            outbound_query = (
                query[: self._max_chunk_chars] if self._max_chunk_chars else query
            )

        self._audit(
            purpose="rerank",
            request_id=rid,
            username=username,
            extra={
                "doc_count": len(outbound_docs),
                "model": model or self._settings.rerank_model,
            },
        )

        url = (
            "https://dashscope.aliyuncs.com/api/v1/services/"
            "rerank/text-rerank/text-rerank"
        )
        body = {
            "model": model or self._settings.rerank_model,
            "input": {"query": outbound_query, "documents": outbound_docs},
            "parameters": {
                "top_n": min(top_n, len(outbound_docs)),
                "return_documents": False,
            },
        }
        req = Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"rerank HTTP {exc.code}: {err[:300]}") from exc
        except URLError as exc:
            raise RuntimeError(f"rerank network error: {exc.reason}") from exc

        output = payload.get("output") or payload
        results = output.get("results") or []
        ranked: list[tuple[int, float]] = []
        for item in results:
            try:
                idx = int(item.get("index"))
                score = float(
                    item.get("relevance_score") or item.get("score") or 0.0
                )
            except (TypeError, ValueError):
                continue
            ranked.append((idx, score))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked


def get_model_gateway(settings: Settings | None = None) -> ModelGateway:
    """Process singleton gateway bound to current settings."""
    global _gateway
    with _lock:
        if _gateway is None:
            if settings is None:
                from src.config import get_settings

                settings = get_settings()
            _gateway = ModelGateway(settings)
        return _gateway


def reset_model_gateway() -> None:
    """Test helper: drop singleton."""
    global _gateway
    with _lock:
        _gateway = None
