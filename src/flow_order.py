"""Rule-based multi-turn flow: 查订单（SQLite orders 表）.

States:
  idle → (intent) → waiting_order_id → (got id) → result → idle
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.tools.order import list_order_ids, lookup_order

# Intent cues for starting the flow.
ORDER_INTENT_MARKERS: tuple[str, ...] = (
    "查订单",
    "查询订单",
    "订单查询",
    "查一下订单",
    "查下订单",
    "我的订单",
    "查物流",
    "查一下物流",
    "查下物流",
    "查询物流",
    "物流到哪",
    "快递到哪",
    "快递到哪儿",
    "物流进度",
    "发货了吗",
    "到哪了",
    "查一下快递",
    "查快递",
)

CANCEL_MARKERS: tuple[str, ...] = (
    "取消",
    "算了",
    "不查了",
    "不用了",
    "退出",
)

_ORDER_ID_RE = re.compile(
    r"(?:订单号|单号|订单)?\s*[:：#]?\s*(ORD\d{4,}|\d{8,}|[A-Z]{2,}\d{6,})",
    re.IGNORECASE,
)
_BARE_ORDER_RE = re.compile(r"^(ORD\d{4,}|\d{8,}|[A-Z]{2,}\d{6,})$", re.IGNORECASE)


@dataclass
class FlowResult:
    handled: bool
    answer: str = ""
    route: str = "flow"
    strategy: str = ""
    active: bool = False


@dataclass
class OrderQueryFlow:
    """Session-scoped state machine for order lookup."""

    state: str = "idle"  # idle | waiting_order_id
    ask_prompt: str = (
        "好的，我来帮您查询订单。请提供订单号"
        "（示例：ORD10001；演示单号还有 ORD10002、ORD10003）。"
        "\n也可以直接说「取消」结束查询。"
    )

    def reset(self) -> None:
        self.state = "idle"

    def handle(self, user_message: str, *, force: bool = False) -> FlowResult:
        text = (user_message or "").strip()
        if not text:
            return FlowResult(handled=False)

        if self.state == "waiting_order_id":
            if _is_cancel(text):
                self.reset()
                return FlowResult(
                    handled=True,
                    answer="已取消订单查询。还有别的可以帮您吗？",
                    strategy="order_cancel",
                    active=False,
                )
            order_id = extract_order_id(text)
            if order_id:
                self.reset()
                return FlowResult(
                    handled=True,
                    answer=format_order_result(order_id),
                    strategy="order_result",
                    active=False,
                )
            return FlowResult(
                handled=True,
                answer=(
                    "没识别到有效订单号。请再发一次"
                    "（如 ORD10001），或说「取消」退出。"
                ),
                strategy="order_slot",
                active=True,
            )

        # idle: start only when intent matches (or forced by LLM intent router)
        if not force and not looks_like_order_intent(text):
            return FlowResult(handled=False)

        order_id = extract_order_id(text)
        if order_id:
            self.reset()
            return FlowResult(
                handled=True,
                answer=format_order_result(order_id),
                strategy="order_result",
                active=False,
            )

        self.state = "waiting_order_id"
        return FlowResult(
            handled=True,
            answer=self.ask_prompt,
            strategy="order_slot",
            active=True,
        )


def looks_like_order_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    if _BARE_ORDER_RE.match(text.strip()):
        return True
    if any(m.lower() in text for m in ORDER_INTENT_MARKERS):
        return True
    # Loose: 查/查询 + 订单/物流/快递
    has_query = any(w in text for w in ("查", "查询", "看看"))
    has_obj = any(w in text for w in ("订单", "物流", "快递", "运单"))
    return has_query and has_obj


def _is_cancel(message: str) -> bool:
    text = (message or "").strip().lower()
    return any(m in text for m in CANCEL_MARKERS)


def extract_order_id(message: str) -> str | None:
    text = (message or "").strip()
    if not text:
        return None
    bare = _BARE_ORDER_RE.match(text)
    if bare:
        return bare.group(1).upper()
    match = _ORDER_ID_RE.search(text)
    if match:
        return match.group(1).upper()
    return None


def format_order_result(order_id: str) -> str:
    oid = order_id.upper()
    data = lookup_order(oid)
    if not data:
        known = "、".join(list_order_ids()) or "（暂无演示单号）"
        return (
            f"未查询到订单「{oid}」。"
            f"当前为演示数据，可用单号：{known}。"
            "\n您可以换一个单号再试，或输入「转人工」。"
        )
    return (
        f"已查到订单 **{oid}**（演示数据）：\n"
        f"- 状态：{data['status']}\n"
        f"- 承运商：{data['carrier']}\n"
        f"- 运单号：{data['tracking_no']}\n"
        f"- 进度：{data['last_event']}\n"
        f"- 时效：{data['eta']}\n\n"
        "如需继续查询其他订单，请再说「查订单」。"
    )
