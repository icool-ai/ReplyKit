"""Feishu Task Center query skill (my_tasks)."""

from __future__ import annotations

from src.skills.base import SkillContext, SkillMeta, SkillOutcome


class FeishuTaskSkill:
    meta = SkillMeta(
        name="feishu_task",
        description="查询飞书任务：我的任务、按成员、任务清单与看板分组",
        routes=("feishu_task_query",),
    )

    def run(self, ctx: SkillContext) -> SkillOutcome:
        from src.chatbot import ChatResult

        force = ctx.force_flow
        bot = ctx.bot
        public_base = ""
        if ctx.channel_ctx is not None:
            public_base = getattr(ctx.channel_ctx, "public_base", "") or ""
        if not public_base:
            public_base = (bot.settings.asset_base_url or "").rstrip("/")

        task_slots = None
        item = ctx.intent.feishu_task_item() if ctx.intent is not None else None
        if item is not None and item.task_scope:
            task_slots = {
                "task_scope": item.task_scope,
                "person_name": item.person_name,
                "tasklist_name": item.tasklist_name,
                "completed": item.completed,
            }

        flow = bot.feishu_task_flow.handle(
            ctx.user_message,
            force=force,
            channel_ctx=ctx.channel_ctx or bot.channel_ctx,
            public_base=public_base,
            channels_db_path=bot.settings.channels_db_path,
            task_slots=task_slots,
        )
        if not flow.handled:
            return SkillOutcome(handled=False, skill_name=self.meta.name)

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
