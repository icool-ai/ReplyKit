"""Rules for transferring conversations to human agents."""

from __future__ import annotations

import re

from src.bot_config import get_bot_config


def should_handoff(user_message: str) -> bool:
    text = user_message.strip().lower()
    keywords = get_bot_config().handoff_keywords
    return any(keyword.lower() in text for keyword in keywords)


def handoff_reply() -> str:
    return get_bot_config().handoff_reply


def no_knowledge_reply() -> str:
    return get_bot_config().no_answer


def handoff_after_no_answer() -> int:
    return max(1, int(getattr(get_bot_config(), "handoff_after_no_answer", 3) or 3))


def handoff_after_repeat() -> int:
    return max(1, int(getattr(get_bot_config(), "handoff_after_repeat", 3) or 3))


def normalize_for_repeat(message: str) -> str:
    """Normalize user text for repeated-question detection."""
    text = (message or "").strip().lower()
    text = re.sub(r"[\s\?？!！。．\.、，,；;：:\-—_…]+", "", text)
    return text


def auto_handoff_reply(reason: str) -> str:
    """Handoff copy with a short reason prefix."""
    base = handoff_reply()
    if reason == "no_answer":
        prefix = (
            "很抱歉，连续多次未能从知识库找到答案。"
            "已为您转接人工客服，请稍候。"
        )
    elif reason == "repeat":
        prefix = (
            "检测到您反复询问同一问题，智能客服可能无法更好解决。"
            "已为您转接人工客服，请稍候。"
        )
    else:
        prefix = "已为您转接人工客服，请稍候。"
    # Avoid duplicating the full script if base already starts similarly.
    if base and base not in prefix:
        return f"{prefix}\n\n{base}"
    return prefix
