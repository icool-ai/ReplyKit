"""Chitchat whitelist for casual greetings (skip RAG)."""

from __future__ import annotations

import re

from src.bot_config import get_bot_config
from src.sensitive_store import get_sensitive_store


def _normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[\s\u3000，。！？、；：,.!?;:~\-—…]+", "", text)
    return text


def is_chitchat(user_message: str) -> bool:
    normalized = _normalize(user_message)
    if not normalized:
        return False
    phrases = get_bot_config().chitchat_phrases
    return normalized in {_normalize(p) for p in phrases}


def chitchat_reply() -> str:
    return get_bot_config().chitchat_reply


def contains_sensitive(user_message: str) -> bool:
    """Return True if message hits a sensitive word or AND-pattern.

    Patterns (from ``data/sensitive.db``, enabled rows only):
      - plain string: substring match after normalizing punctuation
      - ``代开+发票`` / ``代开&发票``: all parts must appear (order-independent)
    """
    text = _normalize(user_message)
    if not text:
        return False

    for raw in get_sensitive_store().enabled_patterns():
        raw = str(raw).strip()
        if not raw:
            continue
        if "+" in raw or "&" in raw:
            parts = [_normalize(p) for p in re.split(r"[+&]", raw)]
            parts = [p for p in parts if p]
            if parts and all(part in text for part in parts):
                return True
            continue
        needle = _normalize(raw)
        if needle and needle in text:
            return True
    return False


def sensitive_reply() -> str:
    return get_bot_config().sensitive_reply


def welcome_message() -> str:
    return get_bot_config().welcome
