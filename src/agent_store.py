"""Official agent marketplace catalog (SQLAlchemy)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from mp_agent.dao._helpers import dt_to_unix, utc_now
from mp_agent.dao._engine_normalize import normalize_store_engine
from mp_agent.dao.models import Agent
from mp_agent.dao.sync_db import sync_engine


@dataclass(frozen=True)
class AgentRow:
    id: str
    name: str
    description: str
    icon: str
    category: str
    runtime: str
    enabled: bool
    sort_order: int
    updated_at: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "category": self.category,
            "runtime": self.runtime,
            "enabled": self.enabled,
            "sort_order": self.sort_order,
            "updated_at": self.updated_at,
        }


_SEED: list[dict[str, Any]] = [
    {
        "id": "customer_service",
        "name": "智能客服",
        "description": "通义 + RAG 智能客服，支持 FAQ、订单、工单与飞书任务等。",
        "icon": "chat",
        "category": "客服",
        "runtime": "replykit_chat",
        "enabled": True,
        "sort_order": 10,
    },
    {
        "id": "ecommerce_competitor",
        "name": "电商竞品分析",
        "description": (
            "多平台自然语言竞品搜索、数据采集、AI 评论摘要与 CSV 导出，"
            "结果持久化到 SQLAlchemy 供趋势分析。"
        ),
        "icon": "shopping",
        "category": "电商调研",
        "runtime": "mp_agent",
        "enabled": True,
        "sort_order": 20,
    },
]


def _row_to_agent(agent: Agent) -> AgentRow:
    return AgentRow(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        icon=agent.icon,
        category=agent.category,
        runtime=agent.runtime,
        enabled=agent.enabled,
        sort_order=agent.sort_order,
        updated_at=dt_to_unix(agent.updated_at),
    )


class AgentStore:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = normalize_store_engine(engine)
        self._seed_if_empty()

    def _seed_if_empty(self) -> None:
        with Session(self._engine) as session:
            count = session.query(Agent).count()
            if count > 0:
                return
            now = utc_now()
            for item in _SEED:
                session.add(
                    Agent(
                        id=item["id"],
                        name=item["name"],
                        description=item["description"],
                        icon=item["icon"],
                        category=item["category"],
                        runtime=item["runtime"],
                        enabled=bool(item["enabled"]),
                        sort_order=int(item["sort_order"]),
                        updated_at=now,
                    )
                )
            session.commit()

    def list_agents(self, *, enabled_only: bool = False) -> list[AgentRow]:
        with Session(self._engine) as session:
            stmt = select(Agent).order_by(Agent.sort_order, Agent.id)
            if enabled_only:
                stmt = stmt.where(Agent.enabled.is_(True))
            rows = session.execute(stmt).scalars().all()
            return [_row_to_agent(a) for a in rows]

    def get(self, agent_id: str) -> AgentRow | None:
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return None
        with Session(self._engine) as session:
            agent = session.get(Agent, agent_id)
            return _row_to_agent(agent) if agent else None

    def update(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        category: str | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> AgentRow | None:
        agent_id = (agent_id or "").strip()
        if not agent_id:
            return None
        with Session(self._engine) as session:
            agent = session.get(Agent, agent_id)
            if agent is None:
                return None
            if name is not None:
                agent.name = name
            if description is not None:
                agent.description = description
            if icon is not None:
                agent.icon = icon
            if category is not None:
                agent.category = category
            if enabled is not None:
                agent.enabled = bool(enabled)
            if sort_order is not None:
                agent.sort_order = int(sort_order)
            agent.updated_at = utc_now()
            session.commit()
            return _row_to_agent(agent)
