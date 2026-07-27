"""LLM intent classification for customer-service routing."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_openai import ChatOpenAI

from src.config import Settings

logger = logging.getLogger(__name__)

IntentType = Literal[
    "faq",
    "order_query",
    "ticket_create",
    "chitchat",
    "handoff",
    "unknown",
]

_VALID_TYPES = {
    "faq",
    "order_query",
    "ticket_create",
    "chitchat",
    "handoff",
    "unknown",
}

INTENT_SYSTEM = """你是客服系统的意图分类器。根据用户一句话，拆出全部意图（可多个），只输出 JSON。

意图类型（type）只能是：
- faq：咨询操作/政策/功能说明（如怎么改地址、如何开票、支付方式有哪些、怎么查看物流入口）
- order_query：要查「自己这单」的订单/物流进度（需要或即将需要订单号），不是问「怎么查」的操作说明
- ticket_create：要创建工单/投诉/报修登记
- chitchat：问候寒暄，与业务无关
- handoff：明确要求转人工
- unknown：无法判断

规则：
1. 「怎么查看物流 / 如何查物流」→ faq（操作说明）；「我的快递到哪了 / 查一下 ORD123 物流」→ order_query
2. 一句话里多个问题时，每个问题一条 intents
3. type=faq 时必须给 search_query：改写成适合检索 FAQ 的短问句（去掉口语废话）
4. 非 faq 的 search_query 可为空字符串
5. 不要回答用户，不要解释，只输出 JSON

输出格式严格如下（不要 markdown 代码块）：
{"intents":[{"type":"faq","search_query":"如何修改收货地址","confidence":0.9}],"primary":"faq"}
"""


@dataclass
class IntentItem:
    type: IntentType
    search_query: str = ""
    confidence: float = 0.0


@dataclass
class IntentResult:
    intents: list[IntentItem] = field(default_factory=list)
    primary: IntentType = "unknown"
    raw: str = ""
    ok: bool = False
    error: str = ""

    @property
    def types(self) -> set[str]:
        return {i.type for i in self.intents}

    @property
    def faq_queries(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in self.intents:
            if item.type != "faq":
                continue
            q = (item.search_query or "").strip()
            if not q or q in seen:
                continue
            seen.add(q)
            out.append(q)
        return out


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty intent response")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"no json object: {raw[:200]}")
    return json.loads(raw[start : end + 1])


def _parse_intent_payload(payload: dict[str, Any], fallback_query: str) -> IntentResult:
    items_raw = payload.get("intents")
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError("intents missing")
    items: list[IntentItem] = []
    for row in items_raw:
        if not isinstance(row, dict):
            continue
        typ = str(row.get("type") or "unknown").strip().lower()
        if typ not in _VALID_TYPES:
            typ = "unknown"
        q = str(row.get("search_query") or "").strip()
        if typ == "faq" and not q:
            q = fallback_query
        try:
            conf = float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        items.append(
            IntentItem(type=typ, search_query=q, confidence=conf)  # type: ignore[arg-type]
        )
    if not items:
        raise ValueError("no valid intents")
    primary_raw = str(payload.get("primary") or items[0].type).strip().lower()
    if primary_raw in _VALID_TYPES:
        primary = primary_raw  # type: ignore[assignment]
    else:
        primary = items[0].type
    return IntentResult(intents=items, primary=primary, ok=True)


def classify_intent(
    settings: Settings,
    user_message: str,
    *,
    dialogue: str = "",
) -> IntentResult:
    """Call chat model to classify intents. On failure returns ok=False."""
    text = (user_message or "").strip()
    if not text:
        return IntentResult(ok=False, error="empty message")

    model = (settings.intent_model or settings.chat_model).strip() or settings.chat_model
    llm = ChatOpenAI(
        model=model,
        openai_api_key=settings.dashscope_api_key,
        openai_api_base=settings.openai_api_base,
        temperature=0,
    )
    human = f"用户消息：{text}"
    if dialogue and dialogue.strip() and dialogue.strip() != "（无）":
        human = f"最近对话：\n{dialogue.strip()}\n\n{human}"

    try:
        resp = llm.invoke(
            [
                ("system", INTENT_SYSTEM),
                ("human", human),
            ]
        )
        content = resp.content if hasattr(resp, "content") else str(resp)
        raw = str(content).strip()
        payload = _extract_json(raw)
        result = _parse_intent_payload(payload, fallback_query=text)
        result.raw = raw[:500]
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("intent classify failed: %s", exc)
        return IntentResult(ok=False, error=str(exc)[:200], raw="")


def route_from_intent(result: IntentResult) -> str:
    """Decide top-level route from classified intents.

    Priority: handoff > faq (if any) > order_query > ticket_create > chitchat > unknown
    FAQ how-to wins over loose order keywords when both appear.
    """
    if not result.ok or not result.intents:
        return "unknown"
    types = result.types
    if "handoff" in types:
        return "handoff"
    if "faq" in types:
        return "faq"
    if "order_query" in types:
        return "order_query"
    if "ticket_create" in types:
        return "ticket_create"
    if "chitchat" in types:
        return "chitchat"
    return "unknown"
