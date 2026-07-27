"""Ops metrics from chat logs (P3-2).

Extensible registry: add a new ``@register_metric`` function to expose more
indicators via ``GET /metrics`` without changing the API shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.chat_log import ChatLogStore

# Knowledge miss / unrecognized: no hit, model reject, or auto handoff after no-answer.
MISS_WHERE = (
    "route = 'none' OR strategy = 'reject' OR strategy = 'auto_no_answer'"
)
HANDOFF_WHERE = "is_handoff = 1"


@dataclass(frozen=True)
class MetricQuery:
    """Shared query context for all metric calculators."""

    store: ChatLogStore
    start: int | None = None
    end: int | None = None
    top_n: int = 10


@dataclass
class MetricResult:
    """One metric payload. ``kind`` guides clients; extra fields live in ``data``."""

    key: str
    label: str
    kind: str  # rate | top_list | number | ...
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            **self.data,
        }


MetricCompute = Callable[[MetricQuery], MetricResult]


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    compute: MetricCompute


_REGISTRY: dict[str, MetricSpec] = {}


def register_metric(key: str, label: str) -> Callable[[MetricCompute], MetricCompute]:
    """Decorator to register a metric calculator under ``key``."""

    def decorator(fn: MetricCompute) -> MetricCompute:
        if key in _REGISTRY:
            raise ValueError(f"metric already registered: {key}")
        _REGISTRY[key] = MetricSpec(key=key, label=label, compute=fn)
        return fn

    return decorator


def list_metric_keys() -> list[str]:
    return list(_REGISTRY.keys())


def resolve_metric_keys(names: list[str] | None) -> list[str]:
    """Return ordered keys to compute. Empty/None → all registered (stable order)."""
    all_keys = list(_REGISTRY.keys())
    if not names:
        return all_keys
    wanted = [n.strip() for n in names if n and n.strip()]
    if not wanted:
        return all_keys
    unknown = [k for k in wanted if k not in _REGISTRY]
    if unknown:
        raise KeyError(
            f"未知指标: {', '.join(unknown)}；可选: {', '.join(all_keys)}"
        )
    # Preserve request order; drop duplicates.
    seen: set[str] = set()
    ordered: list[str] = []
    for k in wanted:
        if k not in seen:
            seen.add(k)
            ordered.append(k)
    return ordered


def compute_metrics(
    store: ChatLogStore,
    *,
    names: list[str] | None = None,
    start: int | None = None,
    end: int | None = None,
    top_n: int = 10,
) -> dict[str, Any]:
    """Compute selected metrics; returns API-ready dict (flat + metrics list)."""
    keys = resolve_metric_keys(names)
    top_n = max(1, min(int(top_n), 100))
    query = MetricQuery(store=store, start=start, end=end, top_n=top_n)
    total = store.count_turns(start=start, end=end)
    results = [_REGISTRY[k].compute(query) for k in keys]
    return {
        "start": start,
        "end": end,
        "total_turns": total,
        "metrics": [r.to_dict() for r in results],
    }


def _rate_result(
    *,
    key: str,
    label: str,
    count: int,
    total: int,
) -> MetricResult:
    value = (count / total) if total > 0 else 0.0
    return MetricResult(
        key=key,
        label=label,
        kind="rate",
        data={
            "value": round(value, 6),
            "count": count,
            "total": total,
        },
    )


@register_metric("miss_rate", "未命中率")
def metric_miss_rate(query: MetricQuery) -> MetricResult:
    total = query.store.count_turns(start=query.start, end=query.end)
    count = query.store.count_matching(
        MISS_WHERE,
        start=query.start,
        end=query.end,
    )
    return _rate_result(
        key="miss_rate",
        label="未命中率",
        count=count,
        total=total,
    )


@register_metric("handoff_rate", "转人工率")
def metric_handoff_rate(query: MetricQuery) -> MetricResult:
    total = query.store.count_turns(start=query.start, end=query.end)
    count = query.store.count_matching(
        HANDOFF_WHERE,
        start=query.start,
        end=query.end,
    )
    return _rate_result(
        key="handoff_rate",
        label="转人工率",
        count=count,
        total=total,
    )


@register_metric("top_unrecognized", "Top 未识别问法")
def metric_top_unrecognized(query: MetricQuery) -> MetricResult:
    pairs = query.store.top_questions_matching(
        MISS_WHERE,
        limit=query.top_n,
        start=query.start,
        end=query.end,
    )
    return MetricResult(
        key="top_unrecognized",
        label="Top 未识别问法",
        kind="top_list",
        data={
            "limit": query.top_n,
            "items": [
                {"question": q, "count": c} for q, c in pairs
            ],
        },
    )
