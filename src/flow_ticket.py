"""Rule-based multi-turn flow: 创建工单（SQLite tickets 表）.

States:
  idle → (intent) → waiting_description → (got text) → create → idle
"""

from __future__ import annotations

from dataclasses import dataclass

from src.flow_order import CANCEL_MARKERS, FlowResult, extract_order_id
from src.tools.ticket import create_ticket

TICKET_INTENT_MARKERS: tuple[str, ...] = (
    "创建工单",
    "提交工单",
    "开个工单",
    "开工单",
    "建工单",
    "报修",
    "我要报修",
    "投诉",
    "我要投诉",
    "登记问题",
    "反馈问题",
)


@dataclass
class TicketCreateFlow:
    """Session-scoped state machine for ticket creation."""

    state: str = "idle"  # idle | waiting_description
    ask_prompt: str = (
        "好的，我来帮您创建工单。请用一两句话描述问题"
        "（可附带订单号，如：门禁刷不开，订单 ORD10001）。"
        "\n也可以直接说「取消」结束。"
    )

    def reset(self) -> None:
        self.state = "idle"

    def handle(self, user_message: str, *, force: bool = False) -> FlowResult:
        text = (user_message or "").strip()
        if not text:
            return FlowResult(handled=False)

        if self.state == "waiting_description":
            if _is_cancel(text):
                self.reset()
                return FlowResult(
                    handled=True,
                    answer="已取消创建工单。还有别的可以帮您吗？",
                    strategy="ticket_cancel",
                    active=False,
                )
            if looks_like_ticket_intent(text) and len(text) < 20:
                return FlowResult(
                    handled=True,
                    answer=self.ask_prompt,
                    strategy="ticket_slot",
                    active=True,
                )
            return self._create_from_description(text)

        if not force and not looks_like_ticket_intent(text):
            return FlowResult(handled=False)

        # Intent + description in one message:「创建工单：门禁刷不开」
        description = _strip_intent_prefix(text)
        if description and description != text.strip():
            return self._create_from_description(description)
        # Pure intent with optional short leftover — ask for description
        if len(description) < 4:
            self.state = "waiting_description"
            return FlowResult(
                handled=True,
                answer=self.ask_prompt,
                strategy="ticket_slot",
                active=True,
            )
        return self._create_from_description(description)

    def _create_from_description(self, text: str) -> FlowResult:
        order_id = extract_order_id(text)
        ticket_id = create_ticket(text, order_id=order_id)
        self.reset()
        extra = f"\n- 关联订单：{order_id}" if order_id else ""
        answer = (
            f"工单已创建成功（演示数据）：\n"
            f"- 工单号：**{ticket_id}**\n"
            f"- 状态：open\n"
            f"- 问题描述：{text.strip()}"
            f"{extra}\n\n"
            "我们会尽快处理。如需查询订单可说「查订单」，"
            "或输入「转人工」。"
        )
        return FlowResult(
            handled=True,
            answer=answer,
            strategy="ticket_created",
            active=False,
        )


def looks_like_ticket_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    return any(m.lower() in text for m in TICKET_INTENT_MARKERS)


def _is_cancel(message: str) -> bool:
    text = (message or "").strip().lower()
    return any(m in text for m in CANCEL_MARKERS)


def _strip_intent_prefix(message: str) -> str:
    """Remove leading intent phrase; keep the rest as description."""
    text = (message or "").strip()
    lower = text.lower()
    # Longest match first
    markers = sorted(TICKET_INTENT_MARKERS, key=len, reverse=True)
    for marker in markers:
        m = marker.lower()
        idx = lower.find(m)
        if idx == 0:
            rest = text[len(marker) :].lstrip(" ：:，,.-—")
            return rest.strip()
    return text
