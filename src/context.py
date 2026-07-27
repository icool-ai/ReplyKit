"""Session context: follow-up detection + query rewrite for retrieval."""

from __future__ import annotations

import re
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from src.bot_config import get_bot_config

# Explicit deixis / continuation cues (substring).
DEFAULT_FOLLOWUP_MARKERS: tuple[str, ...] = (
    "那个",
    "这个",
    "上面说的",
    "刚才说的",
    "刚才",
    "然后呢",
    "还有呢",
    "还有吗",
    "详细点",
    "再说说",
    "具体点",
    "继续",
    "那怎么",
    "那啥",
    "下一步",
    "第二步",
    "上一步",
    "前一步",
)

# Entire message is a short follow-up with no standalone topic.
_SHORT_FOLLOWUP_RE = re.compile(
    r"^(怎么弄|咋弄|怎么做|咋办|怎么办|详细点|具体点|再说说|继续|呢)"
    r"[？?。.!！]*$"
)

# Short continuation when session already has a topic (e.g.「手机端呢」「第二步」).
_CONTEXT_CONTINUATION_RE = re.compile(
    r"^(?:"
    r"(?:那|还|再|另外|对了).{0,14}"
    r"|.{1,12}呢"
    r"|(?:下一步|第二步|第三步|上一步|前一步).{0,8}"
    r"|(?:详细|具体|失败|报错|不行|打不开|进不去).{0,10}"
    r")[？?。.!！]*$"
)

REWRITE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "你是智能客服的「查询改写器」。任务：结合最近对话与已知主题，"
            "把用户当前这句话改写成一个独立、完整、适合检索知识库的中文问题。\n"
            "规则：\n"
            "1. 只输出改写后的问题本身，不要解释、不要回答、不要加引号或前缀。\n"
            "2. 补全省略的主题与指代（那个/这个/它/呢），但不要发明对话里没有的实体。\n"
            "3. 保持用户真实意图；不要扩写成操作步骤或政策说明。\n"
            "4. 若当前句已完整且不依赖上文，原样输出。\n"
            "示例：\n"
            "- 主题=门禁通行，当前=那个怎么弄 → 门禁通行怎么操作\n"
            "- 主题=访客预约，当前=第二步呢 → 访客预约第二步怎么做\n"
            "- 主题=门禁通行，当前=手机端可以吗 → 门禁通行支持手机端吗",
        ),
        (
            "human",
            "已知主题：{topic}\n\n最近对话：\n{history}\n\n当前用户：{question}",
        ),
    ]
)

_SOURCE_TAIL_RE = re.compile(
    r"\n\n(?:📎\s*参考来源：|🖼\s*已附上相关操作截图).*$",
    re.DOTALL,
)


def _cfg_int(name: str, default: int) -> int:
    raw = getattr(get_bot_config(), name, default)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _cfg_bool(name: str, default: bool) -> bool:
    raw = getattr(get_bot_config(), name, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return default


def followup_markers() -> tuple[str, ...]:
    markers = getattr(get_bot_config(), "followup_markers", ()) or ()
    return tuple(markers) if markers else DEFAULT_FOLLOWUP_MARKERS


def looks_like_followup(message: str, *, has_context: bool = False) -> bool:
    """Heuristic: does this utterance likely depend on prior turns?"""
    text = (message or "").strip()
    if not text:
        return False
    if _SHORT_FOLLOWUP_RE.match(text):
        return True
    if any(m in text for m in followup_markers()):
        return True
    # Only when we already know the session topic / have history.
    if has_context and len(text) <= 24 and _CONTEXT_CONTINUATION_RE.match(text):
        return True
    return False


def _clean_assistant_text(content: str) -> str:
    text = _SOURCE_TAIL_RE.sub("", content or "").strip()
    parts = re.split(r"\n\s*\n", text, maxsplit=1)
    return (parts[0] if parts else text).strip()


def _turn_text(role: str, content: str, user_max: int, asst_max: int) -> str:
    body = (content or "").strip()
    if role == "assistant":
        body = _clean_assistant_text(body)
        limit = asst_max
    else:
        limit = user_max
    if limit > 0 and len(body) > limit:
        body = body[:limit].rstrip() + "…"
    label = "用户" if role == "user" else "助手"
    return f"{label}：{body}"


def normalize_history(history: list[Any] | None) -> list[dict[str, str]]:
    """Normalize chat history into [{role, content}, ...]."""
    if not history:
        return []
    out: list[dict[str, str]] = []
    for item in history:
        if isinstance(item, dict):
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "")
            if role == "human":
                role = "user"
            if role == "ai":
                role = "assistant"
            if role in {"user", "assistant"}:
                out.append({"role": role, "content": content})
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            u, a = item[0], item[1]
            if u:
                out.append({"role": "user", "content": str(u)})
            if a:
                out.append({"role": "assistant", "content": str(a)})
    return out


def recent_turns(
    history: list[Any] | None,
    *,
    max_turns: int | None = None,
) -> list[dict[str, str]]:
    """Return last N user-assistant turn pairs flattened (up to 2N messages)."""
    turns = max_turns if max_turns is not None else _cfg_int("history_turns", 3)
    if turns <= 0:
        return []
    msgs = normalize_history(history)
    while msgs and msgs[0]["role"] == "assistant":
        msgs = msgs[1:]
    max_msgs = turns * 2
    return msgs[-max_msgs:] if max_msgs else []


def format_history_for_rewrite(history: list[Any] | None) -> str:
    user_max = _cfg_int("history_user_chars", 80)
    asst_max = _cfg_int("history_assistant_chars", 160)
    turns = recent_turns(history)
    if not turns:
        return "（无）"
    return "\n".join(
        _turn_text(m["role"], m["content"], user_max, asst_max) for m in turns
    )


def topic_from_history(history: list[Any] | None) -> str:
    """Fallback topic: last user question in history."""
    msgs = normalize_history(history)
    for msg in reversed(msgs):
        if msg["role"] == "user":
            text = (msg["content"] or "").strip()
            if text:
                limit = _cfg_int("history_user_chars", 80)
                return text[:limit] if limit else text
    return ""


def topic_from_docs(docs: list[Any]) -> str:
    """Derive a short topic label from retrieved docs for next-turn follow-ups."""
    if not docs:
        return ""
    top = docs[0]
    meta = getattr(top, "metadata", None) or {}
    question = str(meta.get("question") or "").strip()
    if question:
        return question[:40]
    title = str(meta.get("title") or "").strip()
    if title:
        return title[:40]
    source = str(meta.get("source") or "").strip()
    if source:
        name = source.replace("\\", "/").rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if stem:
            return stem[:40]
    content = str(getattr(top, "page_content", "") or "").strip()
    if content:
        first = content.splitlines()[0].strip()
        return first[:40]
    return ""


def concat_with_topic(topic: str, message: str) -> str:
    topic = (topic or "").strip()
    message = (message or "").strip()
    if not topic:
        return message
    if not message:
        return topic
    if topic in message:
        return message
    return f"{topic} {message}"


def rewrite_query(
    llm: ChatOpenAI,
    message: str,
    history: list[Any] | None,
    *,
    topic_hint: str = "",
) -> str:
    """LLM rewrite into a standalone search question; empty string on failure."""
    hist_text = format_history_for_rewrite(history)
    topic = (topic_hint or "").strip() or "（未知）"
    try:
        chain = REWRITE_PROMPT | llm.bind(temperature=0)
        response = chain.invoke(
            {"history": hist_text, "question": message, "topic": topic}
        )
        text = response.content if hasattr(response, "content") else str(response)
        text = str(text).strip().strip('"').strip("'")
        text = re.sub(
            r"^(改写后的?问题|问题|输出)\s*[:：]\s*",
            "",
            text,
        ).strip()
        if "\n" in text:
            text = text.splitlines()[0].strip()
        if not text or len(text) > 200:
            return ""
        return text
    except Exception:  # noqa: BLE001 - fall back to topic concat
        return ""


def resolve_search_query(
    llm: ChatOpenAI,
    message: str,
    history: list[Any] | None = None,
    *,
    last_topic: str = "",
) -> tuple[str, str]:
    """Return (search_query, method) where method is original|rewrite|topic.

    Follow-ups prefer LLM rewrite (feels smarter); topic concat is fallback only.
    Non-follow-ups keep the original text (0 extra tokens).
    """
    text = (message or "").strip()
    if not text:
        return "", "original"

    topic = (last_topic or "").strip() or topic_from_history(history)
    has_context = bool(topic or recent_turns(history))

    if not looks_like_followup(text, has_context=has_context):
        return text, "original"

    rewrite_on = _cfg_bool("rewrite_enabled", True)

    # Prefer intelligent rewrite whenever we have any session signal.
    if rewrite_on and has_context:
        rewritten = rewrite_query(llm, text, history, topic_hint=topic)
        if rewritten:
            return rewritten, "rewrite"

    if topic:
        return concat_with_topic(topic, text), "topic"

    return text, "original"
