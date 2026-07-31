"""RAG-powered customer service chatbot."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from src.chitchat import (
    contains_sensitive,
    sensitive_reply,
)
from src.config import Settings
from src.context import (
    format_history_for_rewrite,
    looks_like_followup,
    resolve_search_query,
    topic_from_docs,
)
from src.flow_order import OrderQueryFlow
from src.flow_ticket import TicketCreateFlow
from src.handoff import (
    auto_handoff_reply,
    handoff_after_no_answer,
    handoff_after_repeat,
    handoff_reply,
    no_knowledge_reply,
    normalize_for_repeat,
    should_handoff,
)
from src.intent import IntentResult, classify_intent, route_from_intent
from src.knowledge import (
    ensure_vectorstore,
    search_faq,
)
from src.tools.business_db import configure_business_db

SYSTEM_PROMPT = """你是一名专业、友好的智能客服助手。

请严格根据以下「参考知识」回答用户问题：
1. 只使用参考知识中的信息；禁止补充、推断或编造知识中未写明的步骤、入口、渠道、时限、条件。
2. 参考知识没写到的细节（例如「在哪里开」「发到邮箱吗」），必须明确说「暂无相关信息」，可建议转人工；不要用常识或猜测填补。
3. 回答简洁清晰，使用中文，语气礼貌；可以润色措辞与分点，但不得改变或增加事实。
4. 不要透露你是 AI 的内部实现细节。
5. 若参考知识附带了操作截图，可在文字中提示用户查看下方配图。
6. 「最近对话」仅用于理解指代与承接上文，不得把对话内容当作事实来源。
7. 若用户在追问细节，请紧扣上一轮主题作答，避免从头重复整段说明。
8. 禁止答非所问：知识只覆盖相关但不同的问题，视为不足以回答，说明暂无相关信息。
9. 若参考知识含多条 FAQ，且用户问题同时涉及其中若干条：只综合知识里真正相关的条目，分点润色；无关条目不要硬凑。

参考知识：
{context}

最近对话：
{dialogue}
"""

MAX_REPLY_IMAGES = 6


def _norm_question(text: str) -> str:
    raw = (text or "").strip().lower()
    return re.sub(r"[\s\?？!！。．\.、，,；;：:\-—_]+", "", raw)


def _faq_can_direct(user_message: str, doc: Document) -> bool:
    """Only blind-paste FAQ when the user ask matches the FAQ/similar phrase closely.

    High embedding score alone is not enough — e.g.「在哪里开发票」≈「怎么开发票」
    but the canned answer may not cover「在哪里」.
    """
    user = _norm_question(user_message)
    if not user:
        return False

    candidates: list[str] = []
    question = str(doc.metadata.get("question") or "").strip()
    if question:
        candidates.append(question)
    match_text = str(doc.metadata.get("match_text") or "").strip()
    if match_text:
        candidates.append(match_text)

    for item in candidates:
        norm = _norm_question(item)
        if not norm:
            continue
        if user == norm or user in norm or norm in user:
            return True
    return False


@dataclass
class ChatResult:
    answer: str
    sources: list[str] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    route: str = ""
    strategy: str = ""
    faq_id: str | None = None
    top_score: float | None = None
    clarify_options: list[str] = field(default_factory=list)
    skill_trace: list[str] = field(default_factory=list)


def _log(title: str, body: str = "") -> None:
    line = "=" * 60

    def _out(text: str) -> None:
        try:
            print(text, flush=True)
        except UnicodeEncodeError:
            # Windows GBK console cannot print some model emojis.
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            print(text.encode(enc, errors="replace").decode(enc, errors="replace"), flush=True)

    _out(f"\n{line}\n{title}")
    if body:
        _out(body)
    _out(line)


def _collect_images(docs: list[Document], limit: int = MAX_REPLY_IMAGES) -> list[str]:
    images: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        raw = doc.metadata.get("images") or []
        if isinstance(raw, str):
            raw = [raw] if raw else []
        for item in raw:
            path = Path(str(item))
            key = str(path.resolve()) if path.exists() else str(item)
            if key in seen:
                continue
            if not path.exists():
                continue
            seen.add(key)
            images.append(str(path.resolve()))
            if len(images) >= limit:
                return images
    return images


def _is_structured_faq(doc: Document) -> bool:
    return (
        doc.metadata.get("doc_type") == "faq"
        and bool(str(doc.metadata.get("answer") or "").strip())
    )


def _clarify_options(docs: list[Document], limit: int) -> list[str]:
    options: list[str] = []
    seen: set[str] = set()
    for doc in docs:
        question = str(doc.metadata.get("question") or "").strip()
        if not question or question in seen:
            continue
        seen.add(question)
        options.append(question)
        if len(options) >= limit:
            break
    return options


def _unique_faq_docs(docs: list[Document]) -> list[Document]:
    """Dedupe structured FAQ hits by faq_id / question (keep retrieval order)."""
    out: list[Document] = []
    seen_id: set[str] = set()
    seen_q: set[str] = set()
    for doc in docs:
        if not _is_structured_faq(doc):
            continue
        faq_id = str(doc.metadata.get("faq_id") or "").strip()
        question = str(doc.metadata.get("question") or "").strip()
        q_key = _norm_question(question)
        if faq_id and faq_id in seen_id:
            continue
        if q_key and q_key in seen_q:
            continue
        if faq_id:
            seen_id.add(faq_id)
        if q_key:
            seen_q.add(q_key)
        out.append(doc)
    return out


def _format_clarify(options: list[str]) -> str:
    if not options:
        return no_knowledge_reply()
    lines = [
        "您可能想问下面这些问题，请点击下方候选，或回复序号 / 输入完整问题：",
        "",
    ]
    for index, question in enumerate(options, 1):
        lines.append(f"{index}. {question}")
    lines.append("")
    lines.append("也可以换个说法再问，或输入「转人工」。")
    return "\n".join(lines)


def _resolve_clarify_pick(message: str, options: list[str]) -> str | None:
    """Map「1」/「2」to a previous clarify option; None if not a pick."""
    text = (message or "").strip()
    if not options or not text:
        return None
    if text.isdigit():
        index = int(text) - 1
        if 0 <= index < len(options):
            return options[index]
    return None


def _context_from_docs(docs: list[Document]) -> str:
    parts: list[str] = []
    for doc in docs:
        if _is_structured_faq(doc):
            q = doc.metadata.get("question", "")
            a = doc.metadata.get("answer", "")
            parts.append(f"【FAQ】Q：{q}\nA：{a}")
        else:
            parts.append(doc.page_content)
    return "\n\n---\n\n".join(parts)


class CustomerServiceBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.llm = ChatOpenAI(
            model=settings.chat_model,
            openai_api_key=settings.dashscope_api_key,
            openai_api_base=settings.openai_api_base,
            temperature=float(settings.answer_temperature),
        )
        self.prompt = ChatPromptTemplate.from_messages(
            [
                ("system", SYSTEM_PROMPT),
                ("human", "{question}"),
            ]
        )
        # Lightweight session memory for follow-up resolution (API / single bot).
        self.last_topic: str = ""
        self.last_clarify_options: list[str] = []
        self.last_effective_query: str = ""
        self.consecutive_no_answer: int = 0
        self.repeat_count: int = 0
        self.last_user_norm: str = ""
        self.order_flow = OrderQueryFlow()
        self.ticket_flow = TicketCreateFlow()
        configure_business_db(settings.business_db_path)
        self._ensure_vectorstore()

    def reset_session(self) -> None:
        self.last_topic = ""
        self.last_clarify_options = []
        self.last_effective_query = ""
        self.consecutive_no_answer = 0
        self.repeat_count = 0
        self.last_user_norm = ""
        self.order_flow.reset()
        self.ticket_flow.reset()

    def dump_session(self) -> dict:
        """Snapshot mutable session fields (for multi-session API)."""
        return {
            "last_topic": self.last_topic,
            "last_clarify_options": list(self.last_clarify_options),
            "last_effective_query": self.last_effective_query,
            "consecutive_no_answer": self.consecutive_no_answer,
            "repeat_count": self.repeat_count,
            "last_user_norm": self.last_user_norm,
            "order_flow_state": self.order_flow.state,
            "ticket_flow_state": self.ticket_flow.state,
        }

    def load_session(self, data: dict | None) -> None:
        """Restore session fields; None / empty → reset."""
        if not data:
            self.reset_session()
            return
        self.last_topic = str(data.get("last_topic") or "")
        opts = data.get("last_clarify_options") or []
        self.last_clarify_options = (
            [str(x) for x in opts] if isinstance(opts, list) else []
        )
        self.last_effective_query = str(data.get("last_effective_query") or "")
        self.consecutive_no_answer = int(data.get("consecutive_no_answer") or 0)
        self.repeat_count = int(data.get("repeat_count") or 0)
        self.last_user_norm = str(data.get("last_user_norm") or "")
        order_state = str(data.get("order_flow_state") or "idle")
        self.order_flow.state = (
            order_state if order_state in {"idle", "waiting_order_id"} else "idle"
        )
        ticket_state = str(data.get("ticket_flow_state") or "idle")
        self.ticket_flow.state = (
            ticket_state
            if ticket_state in {"idle", "waiting_description"}
            else "idle"
        )

    def _try_active_flows(self, user_message: str):
        """Continue in-progress order/ticket flows only."""
        if self.ticket_flow.state != "idle":
            flow = self.ticket_flow.handle(user_message)
            if flow.handled:
                return flow, "ticket_create", self.ticket_flow.state
        if self.order_flow.state != "idle":
            flow = self.order_flow.handle(user_message)
            if flow.handled:
                return flow, "order_query", self.order_flow.state
        return None, "", ""

    def _try_idle_flows(self, user_message: str):
        """Legacy keyword-based flow start (used when intent LLM off/fails)."""
        flow = self.order_flow.handle(user_message)
        if flow.handled:
            return flow, "order_query", self.order_flow.state
        flow = self.ticket_flow.handle(user_message)
        if flow.handled:
            return flow, "ticket_create", self.ticket_flow.state
        return None, "", ""

    def _try_task_flows(self, user_message: str):
        """Active flow first, then idle keyword match (legacy)."""
        active = self._try_active_flows(user_message)
        if active[0] is not None:
            return active
        return self._try_idle_flows(user_message)

    def _search_faq_multi(
        self, queries: list[str]
    ) -> tuple[list[Document], list[tuple[float, str, str]], str]:
        """Run FAQ retrieval for each intent query and merge by faq_id."""
        merged_docs: list[Document] = []
        merged_cands: list[tuple[float, str, str]] = []
        for q in queries:
            q = (q or "").strip()
            if not q:
                continue
            docs, cands, route = search_faq(
                self.settings, q, k=self.settings.top_k
            )
            _log("FAQ 检索问句", q)
            if cands:
                merged_cands.extend(cands)
            if route == "faq" and docs:
                merged_docs.extend(docs)
        if not merged_docs:
            return [], merged_cands, "none"
        merged_docs.sort(
            key=lambda d: float((d.metadata or {}).get("score") or 0.0),
            reverse=True,
        )
        # Prefer unique FAQs; allow up to top_k * num queries but cap reasonably
        unique = _unique_faq_docs(merged_docs)
        limit = max(self.settings.top_k, min(8, self.settings.top_k * max(len(queries), 1)))
        return unique[:limit], merged_cands, "faq"

    def run_faq_turn(
        self,
        user_message: str,
        history: list | None,
        *,
        intent: IntentResult | None = None,
        legacy: bool = False,
    ) -> ChatResult:
        """FAQ retrieval + answer generation (faq_search skill backend)."""
        search_query = user_message.strip()
        resolve_method = "original"
        docs: list[Document] = []
        candidates: list[tuple[float, str, str]] = []
        route = "none"

        if intent is not None:
            queries = intent.faq_queries
            if not queries:
                search_query, resolve_method = resolve_search_query(
                    self.llm,
                    user_message,
                    history,
                    last_topic=self.last_topic,
                )
                queries = [search_query]
            else:
                resolve_method = "intent"
                search_query = " | ".join(queries)
                _log("检索问句(意图)", search_query)
            docs, candidates, route = self._search_faq_multi(queries)
        elif legacy:
            search_query, resolve_method = resolve_search_query(
                self.llm,
                user_message,
                history,
                last_topic=self.last_topic,
            )
            if resolve_method == "original":
                _log("追问改写", "未改写（完整问句或不像追问）")
            else:
                _log(
                    "追问改写",
                    f"method={resolve_method}\n原文：{user_message}\n检索问句：{search_query}"
                    + (
                        f"\nlast_topic：{self.last_topic}"
                        if self.last_topic
                        else ""
                    ),
                )
            docs, candidates, route = search_faq(
                self.settings, search_query, k=self.settings.top_k
            )
        else:
            docs, candidates, route = search_faq(
                self.settings, search_query, k=self.settings.top_k
            )

        return self._finalize_faq_turn(
            user_message,
            history,
            docs,
            candidates,
            route,
            search_query,
            resolve_method,
        )

    def _finalize_faq_turn(
        self,
        user_message: str,
        history: list | None,
        docs: list[Document],
        candidates: list[tuple[float, str, str]],
        route: str,
        search_query: str,
        resolve_method: str,
    ) -> ChatResult:
        _log(
            "检索路由",
            {
                "faq": "采用 FAQ（意图→混合检索"
                + ("+精排" if self.settings.rerank_enabled else "")
                + "）",
                "none": "FAQ 未命中",
            }.get(route, route),
        )
        if docs and (docs[0].metadata or {}).get("_retrieve_debug"):
            _log("混合检索", str((docs[0].metadata or {}).get("_retrieve_debug")))

        clarify_th = self.settings.clarify_threshold
        direct_th = self.settings.direct_threshold
        if candidates:
            score_lines = []
            for i, (score, label, doc_type) in enumerate(candidates, 1):
                if (
                    "[精排" in label
                    or "[未进精排]" in label
                    or "[无关键词" in label
                ):
                    mark = "[已过滤]"
                elif score < clarify_th:
                    mark = "[丢弃:低于澄清阈值]"
                elif doc_type == "faq" and score >= direct_th:
                    mark = "[FAQ可直出]"
                elif score >= clarify_th:
                    mark = "[澄清区间/可采用]"
                else:
                    mark = ""
                score_lines.append(
                    f"  {i}. score={score:.4f} type={doc_type}  {label}  {mark}"
                )
            _log(
                "检索候选（"
                f"澄清阈值={clarify_th}, 直出阈值={direct_th}；实际采用={route}）",
                "\n".join(score_lines),
            )
        else:
            _log("检索候选", "向量库无结果。")

        if not docs or route == "none":
            self.last_clarify_options = []
            self.consecutive_no_answer += 1
            no_th = handoff_after_no_answer()
            _log(
                "检索结果",
                f"FAQ/手册均无片段达到澄清阈值 {clarify_th}。"
                f"（连续无答案 {self.consecutive_no_answer}/{no_th}）",
            )
            if self.consecutive_no_answer >= no_th:
                self._reset_auto_handoff_counters()
                answer = auto_handoff_reply("no_answer")
                _log("结果", f"连续无答案 {no_th} 次，自动转人工。")
                return ChatResult(
                    answer=answer, route="handoff", strategy="auto_no_answer"
                )
            answer = no_knowledge_reply()
            return ChatResult(answer=answer, route="none", strategy="reject")

        topic = topic_from_docs(docs)
        if topic:
            self.last_topic = topic

        sources: list[str] = []
        chunk_logs: list[str] = []
        for i, doc in enumerate(docs, 1):
            source = doc.metadata.get("source", "未知来源")
            doc_type = doc.metadata.get("doc_type", "")
            score = doc.metadata.get("score", "")
            score_text = f"{float(score):.4f}" if score != "" else "?"
            faq_id = doc.metadata.get("faq_id", "")
            question = doc.metadata.get("question", "")
            match_text = doc.metadata.get("match_text", "")
            phrase_role = doc.metadata.get("phrase_role", "")
            images = doc.metadata.get("images") or []
            if isinstance(images, str):
                images = [images] if images else []
            if source not in sources:
                sources.append(source)
            name = Path(str(source)).name
            head = (
                f"[片段 {i}] score={score_text} type={doc_type} "
                f"source={name} images={len(images)}"
            )
            if faq_id:
                head += f" faq_id={faq_id}"
            if phrase_role:
                head += f" hit={phrase_role}:{match_text or doc.page_content}"
            body = (
                f"Q: {question}\nA: {doc.metadata.get('answer', '')}"
                if _is_structured_faq(doc)
                else doc.page_content
            )
            chunk_logs.append(f"{head}\n{body}")
        _log("采用的参考知识", "\n\n".join(chunk_logs))

        top = docs[0]
        top_score = float(top.metadata.get("score") or 0.0)
        top_faq_id = str(top.metadata.get("faq_id") or "") or None
        dialogue = format_history_for_rewrite(history)
        has_prior = bool(dialogue and dialogue != "（无）")
        is_followup = resolve_method != "original" or looks_like_followup(
            user_message, has_context=has_prior
        )
        faq_docs = _unique_faq_docs(docs)
        multi_faq = len(faq_docs) >= 2

        can_direct = _faq_can_direct(user_message, top)
        if (
            _is_structured_faq(top)
            and top_score >= direct_th
            and not is_followup
            and can_direct
            and not multi_faq
        ):
            answer = str(top.metadata.get("answer") or "").strip()
            _log(
                "策略",
                f"FAQ 直出（score={top_score:.4f} >= {direct_th}，问句近匹配）",
            )
            _log("模型原始返回", f"（未调用模型）\n{answer}")
            self.last_clarify_options = []
            self._mark_answered()
            return ChatResult(
                answer=answer,
                sources=sources[:1],
                route="faq",
                strategy="direct",
                faq_id=top_faq_id,
                top_score=top_score,
            )

        if (
            not self.settings.hybrid_search
            and _is_structured_faq(top)
            and top_score < direct_th
            and not is_followup
        ):
            options = _clarify_options(faq_docs or docs, self.settings.clarify_count)
            answer = _format_clarify(options)
            self.last_clarify_options = options
            self._mark_answered()
            _log(
                "策略",
                f"FAQ 澄清（{clarify_th} <= score={top_score:.4f} < {direct_th}）"
                f"；候选={len(options)}",
            )
            _log("模型原始返回", f"（未调用模型）\n{answer}")
            return ChatResult(
                answer=answer,
                sources=sources,
                route="faq",
                strategy="clarify",
                faq_id=top_faq_id,
                top_score=top_score,
                clarify_options=options,
            )

        self.last_clarify_options = []
        gen_docs = docs
        if multi_faq:
            gen_docs = faq_docs
        elif _is_structured_faq(top) and top_score >= direct_th and not can_direct:
            gen_docs = [top]

        context = _context_from_docs(gen_docs)
        if not context.strip():
            self.consecutive_no_answer += 1
            no_th = handoff_after_no_answer()
            answer = no_knowledge_reply()
            _log("检索结果", "内容为空，未调用模型。")
            if self.consecutive_no_answer >= no_th:
                self._reset_auto_handoff_counters()
                answer = auto_handoff_reply("no_answer")
                return ChatResult(
                    answer=answer, route="handoff", strategy="auto_no_answer"
                )
            return ChatResult(answer=answer, route=route, strategy="reject")

        images = _collect_images(gen_docs)
        if images:
            _log("关联配图", "\n".join(f"  - {p}" for p in images))
        else:
            _log("关联配图", "无")

        strategy = "rag"
        if multi_faq:
            strategy = "synthesize"
            _log(
                "策略",
                f"多 FAQ 综合润色（{len(faq_docs)} 条相关知识，模型合并作答）",
            )
        elif _is_structured_faq(top) and top_score >= direct_th and not can_direct:
            strategy = "faq_grounded"
            _log(
                "策略",
                "FAQ 高分但非近匹配：模型核对是否真正答到问题；不足则说明暂无相关信息",
            )
        elif _is_structured_faq(top) and is_followup:
            strategy = "rag_followup"
            _log(
                "策略",
                "追问 + FAQ/文档上下文：结合最近对话生成回答（事实仍只依据参考知识）",
            )
        elif _is_structured_faq(top):
            strategy = "faq_grounded"
            _log(
                "策略",
                "FAQ 命中（含口语改写软命中）：模型依据标准答润色，不足则说明暂无相关信息",
            )
        else:
            _log("策略", "文档 RAG + 结合最近对话生成回答")

        chain = self.prompt | self.llm
        response = chain.invoke(
            {
                "context": context,
                "dialogue": dialogue,
                "question": search_query,
            }
        )
        answer = response.content if hasattr(response, "content") else str(response)
        self._mark_answered()

        _log("模型原始返回", str(answer))
        return ChatResult(
            answer=str(answer),
            sources=sources[:1] if strategy == "faq_grounded" else sources,
            images=images,
            route="faq",
            strategy=strategy,
            faq_id=top_faq_id if _is_structured_faq(top) else None,
            top_score=top_score,
        )

    def _reset_auto_handoff_counters(self) -> None:
        self.consecutive_no_answer = 0
        self.repeat_count = 0
        self.last_user_norm = ""

    def _mark_answered(self) -> None:
        """Successful / usable reply breaks the no-answer streak."""
        self.consecutive_no_answer = 0

    def _ensure_vectorstore(self) -> None:
        msg = ensure_vectorstore(self.settings)
        if msg:
            _log("知识库同步", msg)

    def chat(
        self,
        user_message: str,
        history: list | None = None,
    ) -> tuple[str, list[str], list[str]]:
        result = self.chat_result(user_message, history=history)
        return result.answer, result.sources, result.images

    def chat_result(
        self,
        user_message: str,
        history: list | None = None,
    ) -> ChatResult:
        """Full result including route/strategy metadata (for regression tests)."""
        _log("用户提问", user_message)

        picked = _resolve_clarify_pick(user_message, self.last_clarify_options)
        if picked:
            _log("澄清点选", f"序号/点击映射为标准问：{picked}")
            self.last_clarify_options = []
            user_message = picked
        self.last_effective_query = user_message.strip()

        if should_handoff(user_message):
            self.last_clarify_options = []
            self._reset_auto_handoff_counters()
            answer = handoff_reply()
            _log("结果", "触发转人工关键词，未调用模型。")
            return ChatResult(answer=answer, route="handoff", strategy="fixed")

        # Repeated identical questions → auto handoff.
        norm = normalize_for_repeat(user_message)
        if norm and norm == self.last_user_norm:
            self.repeat_count += 1
        else:
            self.repeat_count = 1
            self.last_user_norm = norm
        repeat_th = handoff_after_repeat()
        if self.repeat_count >= repeat_th:
            self.last_clarify_options = []
            self._reset_auto_handoff_counters()
            answer = auto_handoff_reply("repeat")
            _log(
                "结果",
                f"连续重复提问 {repeat_th} 次，自动转人工。",
            )
            return ChatResult(
                answer=answer, route="handoff", strategy="auto_repeat"
            )

        if contains_sensitive(user_message):
            self.last_clarify_options = []
            self._mark_answered()
            answer = sensitive_reply()
            _log("结果", "命中敏感词，未调用模型。")
            return ChatResult(answer=answer, route="sensitive", strategy="fixed")

        # In-progress 查订单 / 工单：优先续聊，不重新做意图。
        flow, flow_name, flow_state = self._try_active_flows(user_message)
        if flow is not None and flow.handled:
            self.last_clarify_options = []
            self._mark_answered()
            _log(
                "多轮流程",
                f"flow={flow_name} strategy={flow.strategy} state={flow_state}",
            )
            _log("模型原始返回", f"（未调用模型）\n{flow.answer}")
            return ChatResult(
                answer=flow.answer,
                route="flow",
                strategy=flow.strategy,
            )

        dialogue = format_history_for_rewrite(history)
        use_intent = bool(self.settings.intent_llm)

        import src.skills  # noqa: F401 — register built-in skills
        from src.skills.base import SkillContext
        from src.skills.runner import dispatch_intent_route, dispatch_legacy_route

        if use_intent:
            intent = classify_intent(
                self.settings, user_message, dialogue=dialogue
            )
            if intent.ok:
                route_name = route_from_intent(intent)
                intent_lines = [
                    f"  - {it.type} q={it.search_query!r} conf={it.confidence:.2f}"
                    for it in intent.intents
                ]
                _log(
                    "意图识别",
                    f"primary={intent.primary} route={route_name}\n"
                    + "\n".join(intent_lines),
                )
                ctx = SkillContext(
                    bot=self,
                    user_message=user_message,
                    dialogue=dialogue,
                    history=history,
                    intent=intent,
                    route_name=route_name,
                )
                result = dispatch_intent_route(ctx)
                self._log_skill_result(result)
                return result

            _log(
                "意图识别",
                f"失败，回退规则分流：{intent.error or 'unknown'}",
            )

        ctx = SkillContext(
            bot=self,
            user_message=user_message,
            dialogue=dialogue,
            history=history,
        )
        result = dispatch_legacy_route(ctx)
        self._log_skill_result(result)
        return result

    def _log_skill_result(self, result: ChatResult) -> None:
        skills = result.skill_trace or []
        if skills:
            _log("技能轨迹", " → ".join(skills))
        if result.route == "flow":
            _log(
                "多轮流程",
                f"strategy={result.strategy}",
            )
            _log("模型原始返回", f"（未调用模型）\n{result.answer}")
        elif result.route in {"handoff", "chitchat"} and result.strategy in {
            "intent",
            "fixed",
        }:
            _log("结果", f"route={result.route} strategy={result.strategy}")
