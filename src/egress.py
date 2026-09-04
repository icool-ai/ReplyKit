"""Egress sanitization before public model API calls.

Knowledge corpus stays private; only minimized query / chunk text may leave
via the model gateway. This module redacts common PII patterns from outbound text.
"""

from __future__ import annotations

import re
from typing import Iterable

# Phone (CN mobile), national ID (loose), bank-ish long digits, email
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_ID_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
_LONG_DIGIT_RE = re.compile(r"(?<!\d)(\d{13,19})(?!\d)")
_EMAIL_RE = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
)


def redact_pii(text: str) -> str:
    """Best-effort PII redaction for outbound prompts / rerank docs."""
    if not text:
        return text
    out = _EMAIL_RE.sub("[EMAIL]", text)
    out = _ID_RE.sub("[ID]", out)
    out = _PHONE_RE.sub("[PHONE]", out)
    out = _LONG_DIGIT_RE.sub("[NUMBER]", out)
    return out


def sanitize_outbound_texts(
    texts: Iterable[str],
    *,
    max_chars: int | None = None,
) -> list[str]:
    cleaned: list[str] = []
    for raw in texts:
        text = redact_pii(str(raw or ""))
        if max_chars is not None and max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars]
        cleaned.append(text)
    return cleaned


def sanitize_outbound_text(text: str, *, max_chars: int | None = None) -> str:
    items = sanitize_outbound_texts([text], max_chars=max_chars)
    return items[0] if items else ""
