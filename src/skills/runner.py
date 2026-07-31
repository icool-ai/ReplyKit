"""Skill runner: intent route → registered skill → optional FAQ fallback."""

from __future__ import annotations

from src.knowledge import vectorstore_exists
from src.skills.base import SkillContext, SkillOutcome
from src.skills import registry
from src.skills.router import skill_name_for_route


def _run_skill(name: str, ctx: SkillContext) -> SkillOutcome:
    skill = registry.require(name)
    return skill.run(ctx)


def _attach_trace(result, trace: list[str]):
    merged = list(trace)
    for item in result.skill_trace or []:
        if item not in merged:
            merged.append(item)
    result.skill_trace = merged
    return result


def dispatch_intent_route(ctx: SkillContext):
    """After intent classification, invoke the mapped skill (FAQ as fallback)."""
    from src.chatbot import ChatResult, _log

    primary = skill_name_for_route(ctx.route_name)
    trace: list[str] = []

    _log(
        "技能路由",
        f"route={ctx.route_name} → skill={primary}",
    )

    if primary != "faq_search":
        ctx.force_flow = True
        outcome = _run_skill(primary, ctx)
        trace.append(primary)
        _log(
            "技能调用",
            f"skill={primary} handled={outcome.handled}",
        )
        if outcome.handled and outcome.result is not None:
            return _attach_trace(outcome.result, trace)

    if not vectorstore_exists(ctx.bot.settings):
        _log("结果", "向量库不存在，未调用模型。")
        return ChatResult(
            answer="知识库尚未初始化，请先重建知识库。",
            route="uninit",
            strategy="fixed",
            skill_trace=trace + ["faq_search"],
        )

    ctx.via_intent = True
    ctx.legacy_mode = False
    faq_outcome = _run_skill("faq_search", ctx)
    trace.append("faq_search")
    _log("技能调用", "skill=faq_search handled=True")
    if faq_outcome.result is None:
        return ChatResult(
            answer="系统繁忙，请稍后再试。",
            route="error",
            strategy="skill",
            skill_trace=trace,
        )
    return _attach_trace(faq_outcome.result, trace)


def dispatch_legacy_route(ctx: SkillContext):
    """Rule-based routing when intent LLM is off or failed."""
    from src.chatbot import ChatResult, _log

    trace: list[str] = []
    ctx.via_intent = False
    ctx.legacy_mode = True
    ctx.force_flow = False

    for name in ("order_query", "ticket_create"):
        outcome = _run_skill(name, ctx)
        trace.append(name)
        _log(
            "技能调用",
            f"skill={name} handled={outcome.handled}",
        )
        if outcome.handled and outcome.result is not None:
            return _attach_trace(outcome.result, trace)

    chitchat_outcome = _run_skill("chitchat", ctx)
    trace.append("chitchat")
    _log(
        "技能调用",
        f"skill=chitchat handled={chitchat_outcome.handled}",
    )
    if chitchat_outcome.handled and chitchat_outcome.result is not None:
        return _attach_trace(chitchat_outcome.result, trace)

    if not vectorstore_exists(ctx.bot.settings):
        _log("结果", "向量库不存在，未调用模型。")
        return ChatResult(
            answer="知识库尚未初始化，请先重建知识库。",
            route="uninit",
            strategy="fixed",
            skill_trace=trace + ["faq_search"],
        )

    faq_outcome = _run_skill("faq_search", ctx)
    trace.append("faq_search")
    _log("技能调用", "skill=faq_search handled=True")
    if faq_outcome.result is None:
        return ChatResult(
            answer="系统繁忙，请稍后再试。",
            route="error",
            strategy="skill",
            skill_trace=trace,
        )
    return _attach_trace(faq_outcome.result, trace)
