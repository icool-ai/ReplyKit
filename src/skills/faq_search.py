"""FAQ search skill: knowledge retrieval and answer generation."""

from __future__ import annotations

from src.skills.base import SkillContext, SkillMeta, SkillOutcome


class FaqSearchSkill:
    meta = SkillMeta(
        name="faq_search",
        description="检索 FAQ/手册并生成回答",
        routes=("faq", "unknown"),
    )

    def run(self, ctx: SkillContext) -> SkillOutcome:
        result = ctx.bot.run_faq_turn(
            ctx.user_message,
            ctx.history,
            intent=ctx.intent if ctx.via_intent else None,
            legacy=ctx.legacy_mode,
        )
        trace = list(result.skill_trace or [])
        if self.meta.name not in trace:
            trace.insert(0, self.meta.name)
        result.skill_trace = trace
        return SkillOutcome(
            handled=True,
            skill_name=self.meta.name,
            result=result,
        )
