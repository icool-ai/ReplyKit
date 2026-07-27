"""Load / save bot scripts and keyword lists from data/bot_config.json."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.config import PROJECT_ROOT

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "data" / "bot_config.json"
DEFAULT_SCRIPTS_TEMPLATE_PATH = PROJECT_ROOT / "data" / "bot_scripts_template.json"

# Ops-editable script fields (others in bot_config.json stay as engine knobs).
SCRIPT_TEXT_KEYS = (
    "welcome",
    "no_answer",
    "sensitive_reply",
    "handoff_reply",
    "chitchat_reply",
)
SCRIPT_LIST_KEYS = (
    "handoff_keywords",
    "chitchat_phrases",
)
SCRIPT_KEYS = SCRIPT_TEXT_KEYS + SCRIPT_LIST_KEYS


@dataclass(frozen=True)
class BotConfig:
    welcome: str
    no_answer: str
    sensitive_reply: str
    sensitive_words: tuple[str, ...] = field(default_factory=tuple)
    handoff_reply: str = ""
    handoff_keywords: tuple[str, ...] = field(default_factory=tuple)
    chitchat_reply: str = ""
    chitchat_phrases: tuple[str, ...] = field(default_factory=tuple)
    # Session context (P1-1)
    history_turns: int = 3
    history_user_chars: int = 80
    history_assistant_chars: int = 120
    rewrite_enabled: bool = True
    followup_markers: tuple[str, ...] = field(default_factory=tuple)
    # Auto handoff (P1-3)
    handoff_after_no_answer: int = 3
    handoff_after_repeat: int = 3
    path: Path = DEFAULT_CONFIG_PATH


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _as_str_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _normalize_scripts_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize the 7 editable script fields."""
    out: dict[str, Any] = {}
    for key in SCRIPT_TEXT_KEYS:
        text = str(raw.get(key) or "").strip()
        if not text:
            raise ValueError(f"{key} 不能为空")
        out[key] = text
    for key in SCRIPT_LIST_KEYS:
        items = _as_str_tuple(raw.get(key))
        # dedupe preserve order
        seen: set[str] = set()
        uniq: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            uniq.append(item)
        out[key] = uniq
    return out


def scripts_dict_from_config(cfg: BotConfig) -> dict[str, Any]:
    return {
        "welcome": cfg.welcome,
        "no_answer": cfg.no_answer,
        "sensitive_reply": cfg.sensitive_reply,
        "handoff_reply": cfg.handoff_reply,
        "handoff_keywords": list(cfg.handoff_keywords),
        "chitchat_reply": cfg.chitchat_reply,
        "chitchat_phrases": list(cfg.chitchat_phrases),
    }


def _read_json_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"未找到配置文件：{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"须为 JSON 对象：{path}")
    return raw


def load_bot_config(path: Path | None = None) -> BotConfig:
    config_path = (path or DEFAULT_CONFIG_PATH).resolve()
    if not config_path.exists():
        raise FileNotFoundError(
            f"未找到机器人话术配置：{config_path}。"
            "请确认 data/bot_config.json 存在。"
        )

    raw = _read_json_object(config_path)

    return BotConfig(
        welcome=str(raw.get("welcome") or "").strip()
        or "智能对话机器人为您服务，请问有什么可以帮您？",
        no_answer=str(raw.get("no_answer") or "").strip()
        or "抱歉，我暂时没有找到相关信息，请换个问法或输入「转人工」。",
        sensitive_reply=str(raw.get("sensitive_reply") or "").strip()
        or "您说的这个问题我不能回答，请换个问题。",
        # sensitive_words 已迁至 data/sensitive.db；保留字段仅为兼容旧 JSON
        sensitive_words=_as_str_tuple(raw.get("sensitive_words")),
        handoff_reply=str(raw.get("handoff_reply") or "").strip()
        or "已为您标记转人工，请稍候。",
        handoff_keywords=_as_str_tuple(raw.get("handoff_keywords")),
        chitchat_reply=str(raw.get("chitchat_reply") or "").strip()
        or "您好，有什么可以帮您？",
        chitchat_phrases=_as_str_tuple(raw.get("chitchat_phrases")),
        history_turns=max(0, _as_int(raw.get("history_turns"), 3)),
        history_user_chars=max(20, _as_int(raw.get("history_user_chars"), 80)),
        history_assistant_chars=max(
            20, _as_int(raw.get("history_assistant_chars"), 120)
        ),
        rewrite_enabled=_as_bool(raw.get("rewrite_enabled"), True),
        followup_markers=_as_str_tuple(raw.get("followup_markers")),
        handoff_after_no_answer=max(
            1, _as_int(raw.get("handoff_after_no_answer"), 3)
        ),
        handoff_after_repeat=max(1, _as_int(raw.get("handoff_after_repeat"), 3)),
        path=config_path,
    )


@lru_cache(maxsize=4)
def get_bot_config(path: str | None = None) -> BotConfig:
    """Cached loader. Pass absolute path string or None for default."""
    return load_bot_config(Path(path) if path else DEFAULT_CONFIG_PATH)


def reload_bot_config(path: Path | None = None) -> BotConfig:
    get_bot_config.cache_clear()
    return get_bot_config(str(path.resolve()) if path else None)


def get_bot_scripts(path: Path | None = None) -> dict[str, Any]:
    return scripts_dict_from_config(load_bot_config(path))


def load_bot_scripts_template(
    path: Path | None = None,
) -> dict[str, Any]:
    template_path = (path or DEFAULT_SCRIPTS_TEMPLATE_PATH).resolve()
    raw = _read_json_object(template_path)
    return _normalize_scripts_payload(raw)


def save_bot_scripts(
    scripts: dict[str, Any],
    *,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Merge editable scripts into bot_config.json and reload cache."""
    normalized = _normalize_scripts_payload(scripts)
    path = (config_path or DEFAULT_CONFIG_PATH).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raw = _read_json_object(path)
    else:
        raw = {}
    raw.update(normalized)
    path.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    reload_bot_config(path)
    return normalized


def reset_bot_scripts_from_template(
    *,
    config_path: Path | None = None,
    template_path: Path | None = None,
) -> dict[str, Any]:
    template = load_bot_scripts_template(template_path)
    return save_bot_scripts(template, config_path=config_path)
