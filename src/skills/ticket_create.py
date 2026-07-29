"""Ticket create skill: multi-turn complaint / repair registration."""

from __future__ import annotations

from src.skills.base import SkillContext, SkillMeta, SkillOutcome


class TicketCreateSkill:
    meta = SkillMeta(
        name="ticket_create",
        description="创建工单或投诉登记",
        routes=("ticket_create",),
    )

    def run(self, ctx: SkillContext) -> SkillOutcome:
        from src.chatbot import ChatResult

        force = ctx.force_flow
        flow = ctx.bot.ticket_flow.handle(ctx.user_message, force=force)
        if not flow.handled:
            return SkillOutcome(handled=False, skill_name=self.meta.name)

        bot = ctx.bot
        bot.last_clarify_options = []
        bot._mark_answered()
        return SkillOutcome(
            handled=True,
            skill_name=self.meta.name,
            result=ChatResult(
                answer=flow.answer,
                route="flow",
                strategy=flow.strategy,
                skill_trace=[self.meta.name],
            ),
        )
