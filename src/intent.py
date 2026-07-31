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
    "feishu_task_query",
    "chitchat",
    "handoff",
    "unknown",
]

TaskScope = Literal[
    "mine",
    "member",
    "all_visible",
    "list_all",
    "tasklist",
    "board",
    "clarify",
]

_VALID_TYPES = {
    "faq",
    "order_query",
    "ticket_create",
    "feishu_task_query",
    "chitchat",
    "handoff",
    "unknown",
}

_VALID_TASK_SCOPES = {
    "mine",
    "member",
    "all_visible",
    "list_all",
    "tasklist",
    "board",
    "clarify",
}

INTENT_SYSTEM = """你是客服系统的意图分类器。根据用户一句话，拆出全部意图（可多个），只输出 JSON。

意图类型（type）只能是：
- faq：咨询操作/政策/功能说明（如怎么改地址、如何开票、支付方式有哪些、怎么查看物流入口）
- order_query：要查「自己这单」的订单/物流进度（需要或即将需要订单号），不是问「怎么查」的操作说明
- ticket_create：要创建工单/投诉/报修登记（工单≠任务中心）
- feishu_task_query：查询飞书任务中心——我负责的、所有可见任务、某人的任务、任务清单、看板/分组
- chitchat：问候寒暄，与业务无关
- handoff：明确要求转人工
- unknown：无法判断

当 type=feishu_task_query 时必须额外给出槽位：
- task_scope（必填，只能是其一）：
  - mine：查「我负责的 / 我的」任务
  - member：查「某人」负责的任务
  - all_visible：查所有可见任务（不限执行人），如「所有任务」「不是我负责的」
  - list_all：列出有哪些任务清单
  - tasklist：查某个任务清单里的任务
  - board：查某个清单的看板/分组
  - clarify：提到任务但未说明查谁/哪类，需要澄清
- person_name：task_scope=member 时填姓名（如「辰子」「张三」），否则 ""
- tasklist_name：task_scope=tasklist 或 board 时填清单名，否则 ""
- completed：true=只要已完成，false=只要未完成，null=不限

飞书任务判定要点：
- 「帮我看看辰子任务情况」→ member，person_name=辰子（「帮我」不是查我自己）
- 「我有哪些未完成的任务」「我最近的任务情况」→ mine，completed 按语义填
- 「所有任务」「不是我负责的」→ all_visible
- 「看看现在有哪些任务」（未指明范围）→ clarify
- 「有哪些清单」→ list_all
- 「看看【项目A】清单 / 看板」→ tasklist 或 board，并填 tasklist_name

其它规则：
1. 「怎么查看物流 / 如何查物流」→ faq；「我的快递到哪了 / 查一下 ORD123 物流」→ order_query
2. 「创建工单 / 投诉 / 报修」→ ticket_create；任务中心查询 → feishu_task_query（不要与工单混淆）
3. 一句话里多个问题时，每个问题一条 intents
4. type=faq 时必须给 search_query：改写成适合检索 FAQ 的短问句
5. 非 faq 的 search_query 可为空字符串；非 feishu_task_query 时 task_scope/person_name/tasklist_name 用 ""，completed 用 null
6. 不要回答用户，不要解释，只输出 JSON

输出格式严格如下（不要 markdown 代码块）：
{"intents":[{"type":"feishu_task_query","search_query":"","confidence":0.95,"task_scope":"member","person_name":"辰子","tasklist_name":"","completed":null}],"primary":"feishu_task_query"}
"""


@dataclass
class IntentItem:
    type: IntentType
    search_query: str = ""
    confidence: float = 0.0
    task_scope: str = ""
    person_name: str = ""
    tasklist_name: str = ""
    completed: bool | None = None


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

    def feishu_task_item(self) -> IntentItem | None:
        """Prefer primary feishu_task_query item, else first with that type."""
        if not self.ok:
            return None
        primary_hit: IntentItem | None = None
        first_hit: IntentItem | None = None
        for item in self.intents:
            if item.type != "feishu_task_query":
                continue
            if first_hit is None:
                first_hit = item
            if self.primary == "feishu_task_query" and primary_hit is None:
                primary_hit = item
        return primary_hit or first_hit


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


def _parse_completed(raw: Any) -> bool | None:
    if raw is True or raw is False:
        return raw
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _parse_task_scope(raw: Any) -> str:
    scope = str(raw or "").strip().lower()
    return scope if scope in _VALID_TASK_SCOPES else ""


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
        scope = ""
        person = ""
        list_name = ""
        completed: bool | None = None
        if typ == "feishu_task_query":
            scope = _parse_task_scope(row.get("task_scope"))
            person = str(row.get("person_name") or "").strip()
            list_name = str(row.get("tasklist_name") or "").strip()
            completed = _parse_completed(row.get("completed"))
            if scope == "member" and not person:
                # keep scope; flow may fall back to regex extract
                pass
            if scope in {"tasklist", "board"} and not list_name:
                pass
        items.append(
            IntentItem(
                type=typ,  # type: ignore[arg-type]
                search_query=q,
                confidence=conf,
                task_scope=scope,
                person_name=person,
                tasklist_name=list_name,
                completed=completed,
            )
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

    Priority: handoff > faq > order_query > ticket_create > feishu_task_query > chitchat > unknown
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
    if "feishu_task_query" in types:
        return "feishu_task_query"
    if "chitchat" in types:
        return "chitchat"
    return "unknown"
