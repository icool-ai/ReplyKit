"""Built-in agent skills (register on import)."""

from __future__ import annotations

from src.skills.chitchat import ChitchatSkill
from src.skills.faq_search import FaqSearchSkill
from src.skills.feishu_task import FeishuTaskSkill
from src.skills.handoff import HandoffSkill
from src.skills.order_query import OrderQuerySkill
from src.skills import registry
from src.skills.ticket_create import TicketCreateSkill

register = registry.register
get = registry.get
list_skills = registry.list_skills

register(HandoffSkill())
register(ChitchatSkill())
register(OrderQuerySkill())
register(TicketCreateSkill())
register(FeishuTaskSkill())
register(FaqSearchSkill())

from src.skills.runner import dispatch_intent_route, dispatch_legacy_route

__all__ = [
    "dispatch_intent_route",
    "dispatch_legacy_route",
    "get",
    "list_skills",
    "register",
]
