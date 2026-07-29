"""Order query skill: multi-turn order / logistics lookup."""

from __future__ import annotations

from src.skills.base import SkillContext, SkillMeta, SkillOutcome


class OrderQuerySkill:
    meta = SkillMeta(
        name="order_query",
        description="查订单与物流进度",
        routes=("order_query",),
    )

    def run(self, ctx: SkillContext) -> SkillOutcome:
        from src.chatbot import ChatResult

        force = ctx.force_flow
        flow = ctx.bot.order_flow.handle(ctx.user_message, force=force)
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
