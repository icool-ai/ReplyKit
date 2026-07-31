"""Chitchat skill: greetings and non-business small talk."""

from __future__ import annotations

from src.chitchat import chitchat_reply, is_chitchat
from src.skills.base import SkillContext, SkillMeta, SkillOutcome


class ChitchatSkill:
    meta = SkillMeta(
        name="chitchat",
        description="闲聊寒暄回复",
        routes=("chitchat",),
    )

    def run(self, ctx: SkillContext) -> SkillOutcome:
        from src.chatbot import ChatResult

        if ctx.legacy_mode and not is_chitchat(ctx.user_message):
            return SkillOutcome(handled=False, skill_name=self.meta.name)

        bot = ctx.bot
        bot.last_clarify_options = []
        bot._mark_answered()
        strategy = "fixed" if ctx.legacy_mode else "intent"
        return SkillOutcome(
            handled=True,
            skill_name=self.meta.name,
            result=ChatResult(
                answer=chitchat_reply(),
                route="chitchat",
                strategy=strategy,
                skill_trace=[self.meta.name],
            ),
        )
