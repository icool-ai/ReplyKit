"""Response / request shapes for Customer Agent API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class HealthResponse:
    status: str
    redis: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HealthResponse:
        redis = data.get("redis")
        return cls(
            status=str(data.get("status", "")),
            redis=dict(redis) if isinstance(redis, dict) else None,
        )


@dataclass
class ChatRequest:
    session_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"session_id": self.session_id, "message": self.message}


@dataclass
class ChatResponse:
    session_id: str
    answer: str
    sources: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    clarify_options: list[str] = field(default_factory=list)
    route: str = ""
    strategy: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatResponse:
        return cls(
            session_id=str(data.get("session_id", "")),
            answer=str(data.get("answer", "")),
            sources=list(data.get("sources") or []),
            images=list(data.get("images") or []),
            clarify_options=list(data.get("clarify_options") or []),
            route=str(data.get("route") or ""),
            strategy=str(data.get("strategy") or ""),
        )


@dataclass
class ClearSessionResponse:
    status: str
    session_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClearSessionResponse:
        return cls(
            status=str(data.get("status", "")),
            session_id=str(data.get("session_id", "")),
        )
