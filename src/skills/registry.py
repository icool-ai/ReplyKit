"""Skill registry: register built-in skills and look up by name."""

from __future__ import annotations

from src.skills.base import Skill, SkillMeta

_REGISTRY: dict[str, Skill] = {}


def register(skill: Skill) -> Skill:
    """Register a skill; returns the same instance for chaining."""
    name = skill.meta.name
    if not name:
        raise ValueError("skill.meta.name is required")
    _REGISTRY[name] = skill
    return skill


def get(name: str) -> Skill | None:
    return _REGISTRY.get(name)


def require(name: str) -> Skill:
    skill = get(name)
    if skill is None:
        raise KeyError(f"skill not registered: {name}")
    return skill


def list_skills() -> list[SkillMeta]:
    return [s.meta for s in _REGISTRY.values()]


def clear() -> None:
    """Test helper."""
    _REGISTRY.clear()
