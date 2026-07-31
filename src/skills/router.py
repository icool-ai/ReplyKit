"""Map intent routes to skill names."""

from __future__ import annotations

ROUTE_TO_SKILL: dict[str, str] = {
    "handoff": "handoff",
    "chitchat": "chitchat",
    "order_query": "order_query",
    "ticket_create": "ticket_create",
    "feishu_task_query": "feishu_task",
    "faq": "faq_search",
    "unknown": "faq_search",
}


def skill_name_for_route(route_name: str) -> str:
    return ROUTE_TO_SKILL.get(route_name or "unknown", "faq_search")
