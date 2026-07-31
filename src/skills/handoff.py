"""Handoff skill: transfer to human agent."""

from __future__ import annotations

from src.handoff import handoff_reply
from src.skills.base import SkillContext, SkillMeta, SkillOutcome


class HandoffSkill:
    meta = SkillMeta(
        name="handoff",
        description="转接人工客服",
        routes=("handoff",),
    )

    def run(self, ctx: SkillContext) -> SkillOutcome:
        from src.chatbot import ChatResult

        bot = ctx.bot
        bot.last_clarify_options = []
        bot._reset_auto_handoff_counters()
        answer = handoff_reply()
        return SkillOutcome(
            handled=True,
            skill_name=self.meta.name,
            result=ChatResult(
                answer=answer,
                route="handoff",
                strategy="intent",
                skill_trace=[self.meta.name],
            ),
        )
