"""Skill runtime types: context, outcome, and the Skill protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from langchain_core.documents import Document

    from src.chatbot import ChatResult, CustomerServiceBot
    from src.intent import IntentResult


@dataclass
class ChannelContext:
    """IM / web identity for the current turn (set under bot_lock)."""

    channel: str = "web"  # web | feishu
    open_id: str = ""
    feishu_config_id: str = ""
    app_id: str = ""
    app_secret: str = ""
    public_base: str = ""


@dataclass
class SkillContext:
    """Inputs for one skill invocation."""

    bot: CustomerServiceBot
    user_message: str
    dialogue: str = ""
    history: list[Any] | None = None
    intent: IntentResult | None = None
    route_name: str = "unknown"
    channel_ctx: ChannelContext | None = None
    # FAQ retrieval (optional — filled by router or faq skill)
    search_query: str = ""
    resolve_method: str = "original"
    queries: list[str] = field(default_factory=list)
    docs: list[Document] | None = None
    candidates: list[tuple[float, str, str]] | None = None
    retrieve_route: str = ""
    legacy_mode: bool = False
    via_intent: bool = False
    force_flow: bool = False


@dataclass
class SkillOutcome:
    """Result of a skill run."""

    handled: bool
    result: ChatResult | None = None
    skill_name: str = ""


@dataclass(frozen=True)
class SkillMeta:
    """Registry metadata exposed to logs / future tool schemas."""

    name: str
    description: str
    routes: tuple[str, ...] = ()


class Skill(Protocol):
    """One callable capability in the agent runtime."""

    meta: SkillMeta

    def run(self, ctx: SkillContext) -> SkillOutcome: ...
