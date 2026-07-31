"""Rule-based flow: 飞书任务中心（我的 / 按成员 / 清单 / 看板）.

Supports multi-turn clarify when name matches multiple users or tasklists.
Requires Feishu channel context and user OAuth token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from src.channel_store import get_channel_store
from src.channels.feishu_oauth import (
    build_user_authorize_link,
    get_valid_user_access_token,
)
from src.flow_order import CANCEL_MARKERS, FlowResult
from src.tools.feishu_contact import FeishuUserHit, search_users_by_name
from src.tools.feishu_task import (
    filter_tasks_by_assignee,
    format_board,
    format_task_list,
    format_tasklist_names,
    list_my_tasks,
    list_sections,
    list_tasklist_tasks,
    list_tasklists,
    search_tasks,
)

TASK_INTENT_MARKERS: tuple[str, ...] = (
    "我的任务",
    "任务完成情况",
    "任务怎么样",
    "有哪些任务",
    "未完成的任务",
    "已完成的任务",
    "看看任务",
    "查任务",
    "查询任务",
    "任务列表",
    "我的待办",
    "待办任务",
    "任务中心",
    "任务清单",
    "有哪些清单",
    "看看清单",
    "查清单",
    "看板",
    "的任务",
    "的待办",
)

_MEMBER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:帮我)?(?:查一下|查下|看看|查看|查询|看一下|看下)\s*(.+?)\s*的\s*"
        r"(?:未完成|已完成)?\s*(?:任务|待办)(?:情况)?",
        re.I,
    ),
    # 「看看辰子任务情况」——姓名与「任务」之间可无「的」
    re.compile(
        r"(?:帮我)?(?:查一下|查下|看看|查看|查询|看一下|看下)\s*(.+?)\s*"
        r"(?:未完成|已完成)?\s*(?:任务|待办)(?:情况)?",
        re.I,
    ),
    re.compile(r"(.+?)\s*(?:负责的|名下的)\s*(?:任务|待办)(?:情况)?", re.I),
    re.compile(r"(.+?)\s*的\s*(?:未完成|已完成)?\s*(?:任务|待办)(?:情况)?", re.I),
)

_TASKLIST_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[「【\[]\s*(.+?)\s*[」】\]]\s*(?:的)?\s*(?:任务)?\s*(?:清单|看板)", re.I),
    re.compile(r"(?:看看|查看|打开)?\s*(.+?)\s*(?:任务)?清单", re.I),
    re.compile(r"(?:看看|查看|打开)?\s*(.+?)\s*看板", re.I),
)

_SELF_MARKERS = (
    "我的任务",
    "我的待办",
    "我有哪些",
    "我负责",
    "自己的任务",
    "自己的待办",
    "我名下",
)
_ALL_VISIBLE_MARKERS = (
    "所有任务",
    "全部任务",
    "所有的任务",
    "全部的任务",
    "所有可见",
    "不是我负责",
    "不是我的任务",
    "不只是我",
    "大家的任务",
    "全部可见",
)
_LIST_ALL_MARKERS = ("有哪些清单", "任务清单有哪些", "列出清单", "我的清单", "可读清单")


@dataclass
class FeishuTaskFlow:
    """Session-scoped state machine for Feishu task queries."""

    state: str = "idle"  # idle | waiting_member_pick | waiting_tasklist_pick
    pending_kind: str = ""  # member | tasklist | board
    pending_completed: bool | None = None
    pending_users: list[dict[str, str]] = field(default_factory=list)
    pending_tasklists: list[dict[str, str]] = field(default_factory=list)
    want_board: bool = False

    def reset(self) -> None:
        self.state = "idle"
        self.pending_kind = ""
        self.pending_completed = None
        self.pending_users = []
        self.pending_tasklists = []
        self.want_board = False

    def dump(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "pending_kind": self.pending_kind,
            "pending_completed": self.pending_completed,
            "pending_users": list(self.pending_users),
            "pending_tasklists": list(self.pending_tasklists),
            "want_board": self.want_board,
        }

    def load(self, data: dict[str, Any] | None) -> None:
        if not data:
            self.reset()
            return
        state = str(data.get("state") or "idle")
        self.state = (
            state
            if state in {"idle", "waiting_member_pick", "waiting_tasklist_pick"}
            else "idle"
        )
        self.pending_kind = str(data.get("pending_kind") or "")
        raw_c = data.get("pending_completed")
        if raw_c is True or raw_c is False:
            self.pending_completed = raw_c
        else:
            self.pending_completed = None
        users = data.get("pending_users") or []
        self.pending_users = (
            [
                {"open_id": str(u.get("open_id") or ""), "name": str(u.get("name") or "")}
                for u in users
                if isinstance(u, dict)
            ]
            if isinstance(users, list)
            else []
        )
        lists = data.get("pending_tasklists") or []
        self.pending_tasklists = (
            [
                {"guid": str(t.get("guid") or ""), "name": str(t.get("name") or "")}
                for t in lists
                if isinstance(t, dict)
            ]
            if isinstance(lists, list)
            else []
        )
        self.want_board = bool(data.get("want_board"))

    def handle(
        self,
        user_message: str,
        *,
        force: bool = False,
        channel_ctx=None,
        public_base: str = "",
        channels_db_path=None,
        task_slots: dict[str, Any] | None = None,
    ) -> FlowResult:
        text = (user_message or "").strip()
        if not text:
            return FlowResult(handled=False)

        if self.state != "idle":
            if _is_cancel(text):
                self.reset()
                return FlowResult(
                    handled=True,
                    answer="已取消任务查询。",
                    strategy="feishu_task_cancel",
                    active=False,
                )
            auth = self._ensure_auth(
                channel_ctx=channel_ctx,
                public_base=public_base,
                channels_db_path=channels_db_path,
            )
            if isinstance(auth, FlowResult):
                return auth
            token = auth
            if self.state == "waiting_member_pick":
                return self._finish_member_pick(text, token=token)
            if self.state == "waiting_tasklist_pick":
                return self._finish_tasklist_pick(text, token=token)

        # LLM 槽位可在 force 时跳过关键词门槛；无槽位则仍需像任务意图
        slots = _normalize_task_slots(task_slots)
        if not force and not slots and not looks_like_task_intent(text):
            return FlowResult(handled=False)

        kind, completed, person, list_name = resolve_task_query(
            text, slots=slots
        )
        if kind == "clarify":
            return FlowResult(
                handled=True,
                answer=(
                    "想查哪一类任务？可以说：\n"
                    "1. 我负责的（例：「我有哪些未完成的任务」）\n"
                    "2. 所有可见的（例：「所有任务」「不是只看我负责的」）\n"
                    "3. 某人的（例：「查一下张三的任务」）\n"
                    "4. 某清单/看板（例：「看看【项目A】清单」）"
                ),
                strategy="feishu_task_clarify",
                active=False,
            )

        auth = self._ensure_auth(
            channel_ctx=channel_ctx,
            public_base=public_base,
            channels_db_path=channels_db_path,
        )
        if isinstance(auth, FlowResult):
            return auth
        token = auth

        if kind == "list_all":
            return self._query_list_names(token=token)
        if kind == "tasklist" or kind == "board":
            self.want_board = kind == "board" or (
                not slots and "看板" in text
            )
            if slots and kind == "board":
                self.want_board = True
            name = list_name
            if not name:
                return FlowResult(
                    handled=True,
                    answer="请说明清单名称，例如：「看看【项目A】清单」或「项目A看板」。",
                    strategy="feishu_task_need_list_name",
                    active=False,
                )
            return self._query_tasklist(
                token=token,
                name_query=name,
                completed=completed,
                as_board=self.want_board,
            )
        if kind == "member":
            if not person:
                return FlowResult(
                    handled=True,
                    answer="请说明要查谁，例如：「查一下张三的未完成任务」。",
                    strategy="feishu_task_need_member",
                    active=False,
                )
            return self._query_member(
                token=token,
                name_query=person,
                completed=completed,
            )
        if kind == "all_visible":
            return self._query_all_visible(token=token, completed=completed)

        return self._query_mine(token=token, completed=completed)

    def _ensure_auth(
        self,
        *,
        channel_ctx,
        public_base: str,
        channels_db_path,
    ) -> str | FlowResult:
        ctx = channel_ctx
        channel = (getattr(ctx, "channel", None) or "web").strip().lower()
        open_id = (getattr(ctx, "open_id", None) or "").strip()
        config_id = (getattr(ctx, "feishu_config_id", None) or "").strip()
        app_id = (getattr(ctx, "app_id", None) or "").strip()
        app_secret = (getattr(ctx, "app_secret", None) or "").strip()

        if channel != "feishu" or not open_id or not config_id:
            return FlowResult(
                handled=True,
                answer=(
                    "查询飞书任务需要在飞书里对机器人说话"
                    "（例如：「我有哪些未完成的任务」「查一下张三的任务」"
                    "「有哪些清单」「看看【项目A】看板」）。"
                    "网页端暂不支持直接绑定飞书身份。"
                ),
                strategy="feishu_task_need_channel",
                active=False,
            )
        if not app_id or not app_secret:
            return FlowResult(
                handled=True,
                answer="飞书应用凭证不完整，请管理员检查渠道配置中的 App ID / Secret。",
                strategy="feishu_task_no_app",
                active=False,
            )
        if channels_db_path is None:
            return FlowResult(
                handled=True,
                answer="系统未配置渠道库，暂时无法查询任务。",
                strategy="feishu_task_no_store",
                active=False,
            )
        store = get_channel_store(channels_db_path)
        token = get_valid_user_access_token(
            store,
            config_id=config_id,
            open_id=open_id,
            app_id=app_id,
            app_secret=app_secret,
        )
        if token:
            return token
        try:
            link = build_user_authorize_link(
                public_base=public_base
                or getattr(ctx, "public_base", "")
                or "",
                config_id=config_id,
                open_id=open_id,
                app_id=app_id,
            )
        except ValueError as exc:
            return FlowResult(
                handled=True,
                answer=str(exc),
                strategy="feishu_task_oauth_misconfig",
                active=False,
            )
        return FlowResult(
            handled=True,
            answer=(
                "查询任务前需要你授权飞书账号（任务/清单/搜人只读权限）。\n"
                "若以前授权过，权限已升级也请重新点一次：\n"
                f"{link}"
            ),
            strategy="feishu_task_need_oauth",
            active=False,
        )

    def _query_mine(self, *, token: str, completed: bool | None) -> FlowResult:
        try:
            items = list_my_tasks(token, completed=completed, page_size=20)
        except Exception as exc:  # noqa: BLE001
            return FlowResult(
                handled=True,
                answer=f"查询飞书任务失败：{exc}",
                strategy="feishu_task_api_error",
                active=False,
            )
        self.reset()
        return FlowResult(
            handled=True,
            answer=format_task_list(items, completed_filter=completed, owner_label="你"),
            strategy="feishu_task_mine",
            active=False,
        )

    def _query_all_visible(
        self, *, token: str, completed: bool | None
    ) -> FlowResult:
        try:
            items = search_tasks(
                token,
                completed=completed,
                page_size=20,
            )
            if not items:
                items = self._all_tasks_via_lists(token=token, completed=completed)
        except Exception as exc:  # noqa: BLE001
            return FlowResult(
                handled=True,
                answer=f"查询可见任务失败：{exc}",
                strategy="feishu_task_api_error",
                active=False,
            )
        self.reset()
        return FlowResult(
            handled=True,
            answer=format_task_list(
                items,
                completed_filter=completed,
                owner_label="你",
                assignee_scoped=False,
            ),
            strategy="feishu_task_all_visible",
            active=False,
        )

    def _all_tasks_via_lists(
        self,
        *,
        token: str,
        completed: bool | None,
    ) -> list[dict[str, Any]]:
        lists = list_tasklists(token, page_size=50)
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tl in lists[:20]:
            guid = str(tl.get("guid") or "").strip()
            if not guid:
                continue
            try:
                tasks = list_tasklist_tasks(
                    token, guid, completed=completed, page_size=50
                )
            except Exception:  # noqa: BLE001
                continue
            for task in tasks:
                tg = str(task.get("guid") or "").strip()
                key = tg or str(task.get("summary") or "")
                if key in seen:
                    continue
                seen.add(key)
                merged.append(task)
            if len(merged) >= 20:
                break
        return merged[:20]

    def _query_member(
        self,
        *,
        token: str,
        name_query: str,
        completed: bool | None,
    ) -> FlowResult:
        try:
            hits = search_users_by_name(token, name_query)
        except Exception as exc:  # noqa: BLE001
            return FlowResult(
                handled=True,
                answer=f"按姓名搜人失败：{exc}（请确认已开通 contact:user:search 并重新授权）",
                strategy="feishu_task_contact_error",
                active=False,
            )
        if not hits:
            return FlowResult(
                handled=True,
                answer=f"通讯录里没找到「{name_query}」。请换更完整的姓名再试。",
                strategy="feishu_task_member_not_found",
                active=False,
            )
        if len(hits) > 1:
            self.state = "waiting_member_pick"
            self.pending_kind = "member"
            self.pending_completed = completed
            self.pending_users = [
                {"open_id": h.open_id, "name": h.name} for h in hits[:10]
            ]
            lines = ["找到多人，请回复序号选择（或说取消）："]
            for i, h in enumerate(self.pending_users, 1):
                lines.append(f"{i}. {h['name']}")
            return FlowResult(
                handled=True,
                answer="\n".join(lines),
                strategy="feishu_task_member_clarify",
                active=True,
            )
        return self._fetch_member_tasks(
            token=token,
            hit=hits[0],
            completed=completed,
        )

    def _finish_member_pick(self, text: str, *, token: str) -> FlowResult:
        idx = _parse_pick_index(text, len(self.pending_users))
        if idx is None:
            return FlowResult(
                handled=True,
                answer="请回复列表中的序号，或说「取消」。",
                strategy="feishu_task_member_clarify",
                active=True,
            )
        row = self.pending_users[idx]
        hit = FeishuUserHit(open_id=row["open_id"], name=row["name"])
        completed = self.pending_completed
        return self._fetch_member_tasks(token=token, hit=hit, completed=completed)

    def _fetch_member_tasks(
        self,
        *,
        token: str,
        hit: FeishuUserHit,
        completed: bool | None,
    ) -> FlowResult:
        try:
            items = search_tasks(
                token,
                assignee_open_ids=[hit.open_id],
                completed=completed,
                page_size=20,
            )
            if not items:
                # Fallback: scan readable tasklists and filter by assignee.
                items = self._member_tasks_via_lists(
                    token=token,
                    open_id=hit.open_id,
                    completed=completed,
                )
        except Exception as exc:  # noqa: BLE001
            return FlowResult(
                handled=True,
                answer=f"查询「{hit.name}」的任务失败：{exc}",
                strategy="feishu_task_api_error",
                active=False,
            )
        self.reset()
        answer = format_task_list(
            items,
            completed_filter=completed,
            owner_label=hit.name,
        )
        if not items:
            answer += (
                "\n说明：只能看到你有权限的任务；"
                "若对方任务未与你共享，这里会为空。"
            )
        return FlowResult(
            handled=True,
            answer=answer,
            strategy="feishu_task_member",
            active=False,
        )

    def _member_tasks_via_lists(
        self,
        *,
        token: str,
        open_id: str,
        completed: bool | None,
    ) -> list[dict[str, Any]]:
        lists = list_tasklists(token, page_size=50)
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tl in lists[:20]:
            guid = str(tl.get("guid") or "").strip()
            if not guid:
                continue
            try:
                tasks = list_tasklist_tasks(
                    token, guid, completed=completed, page_size=50
                )
            except Exception:  # noqa: BLE001
                continue
            for task in filter_tasks_by_assignee(tasks, open_id):
                tg = str(task.get("guid") or "").strip()
                key = tg or str(task.get("summary") or "")
                if key in seen:
                    continue
                seen.add(key)
                merged.append(task)
            if len(merged) >= 20:
                break
        return merged[:20]

    def _query_list_names(self, *, token: str) -> FlowResult:
        try:
            items = list_tasklists(token, page_size=50)
        except Exception as exc:  # noqa: BLE001
            return FlowResult(
                handled=True,
                answer=f"查询任务清单失败：{exc}",
                strategy="feishu_task_api_error",
                active=False,
            )
        self.reset()
        return FlowResult(
            handled=True,
            answer=format_tasklist_names(items),
            strategy="feishu_task_lists",
            active=False,
        )

    def _query_tasklist(
        self,
        *,
        token: str,
        name_query: str,
        completed: bool | None,
        as_board: bool,
    ) -> FlowResult:
        try:
            items = list_tasklists(token, page_size=50)
        except Exception as exc:  # noqa: BLE001
            return FlowResult(
                handled=True,
                answer=f"查询任务清单失败：{exc}",
                strategy="feishu_task_api_error",
                active=False,
            )
        matched = _match_tasklists(items, name_query)
        if not matched:
            return FlowResult(
                handled=True,
                answer=(
                    f"没找到名称包含「{name_query}」的清单。"
                    "可先说「有哪些清单」看看可读列表。"
                ),
                strategy="feishu_task_list_not_found",
                active=False,
            )
        if len(matched) > 1:
            self.state = "waiting_tasklist_pick"
            self.pending_kind = "board" if as_board else "tasklist"
            self.pending_completed = completed
            self.want_board = as_board
            self.pending_tasklists = [
                {
                    "guid": str(t.get("guid") or ""),
                    "name": str(t.get("name") or "").strip() or "（未命名）",
                }
                for t in matched[:10]
            ]
            lines = ["找到多个清单，请回复序号选择（或说取消）："]
            for i, t in enumerate(self.pending_tasklists, 1):
                lines.append(f"{i}. {t['name']}")
            return FlowResult(
                handled=True,
                answer="\n".join(lines),
                strategy="feishu_task_list_clarify",
                active=True,
            )
        return self._fetch_tasklist_view(
            token=token,
            guid=str(matched[0].get("guid") or ""),
            name=str(matched[0].get("name") or "").strip() or name_query,
            completed=completed,
            as_board=as_board,
        )

    def _finish_tasklist_pick(self, text: str, *, token: str) -> FlowResult:
        idx = _parse_pick_index(text, len(self.pending_tasklists))
        if idx is None:
            return FlowResult(
                handled=True,
                answer="请回复列表中的序号，或说「取消」。",
                strategy="feishu_task_list_clarify",
                active=True,
            )
        row = self.pending_tasklists[idx]
        return self._fetch_tasklist_view(
            token=token,
            guid=row["guid"],
            name=row["name"],
            completed=self.pending_completed,
            as_board=self.want_board or self.pending_kind == "board",
        )

    def _fetch_tasklist_view(
        self,
        *,
        token: str,
        guid: str,
        name: str,
        completed: bool | None,
        as_board: bool,
    ) -> FlowResult:
        if not guid:
            return FlowResult(
                handled=True,
                answer="清单 ID 无效，请重试。",
                strategy="feishu_task_list_not_found",
                active=False,
            )
        try:
            tasks = list_tasklist_tasks(
                token, guid, completed=completed, page_size=50
            )
            if as_board:
                sections = list_sections(
                    token, resource_type="tasklist", resource_id=guid
                )
                answer = format_board(
                    tasklist_name=name,
                    sections=sections,
                    tasks=tasks,
                    completed_filter=completed,
                )
                strategy = "feishu_task_board"
            else:
                answer = format_task_list(
                    tasks,
                    completed_filter=completed,
                    owner_label=f"「{name}」清单中的",
                )
                strategy = "feishu_task_list_detail"
        except Exception as exc:  # noqa: BLE001
            return FlowResult(
                handled=True,
                answer=f"查询清单「{name}」失败：{exc}",
                strategy="feishu_task_api_error",
                active=False,
            )
        self.reset()
        return FlowResult(
            handled=True,
            answer=answer,
            strategy=strategy,
            active=False,
        )


def looks_like_task_intent(message: str) -> bool:
    text = (message or "").strip().lower()
    if not text:
        return False
    if any(k in text for k in ("工单", "投诉", "报修")) and "任务" not in text:
        return False
    if any(m.lower() in text for m in TASK_INTENT_MARKERS):
        return True
    if extract_member_name(text) or extract_tasklist_name(text):
        return True
    if "任务" in text and any(
        k in text for k in ("查", "看", "问", "有哪些", "清单", "看板", "待办")
    ):
        return True
    return False


_VALID_SLOT_SCOPES = {
    "mine",
    "member",
    "all_visible",
    "list_all",
    "tasklist",
    "board",
    "clarify",
}


def _normalize_task_slots(
    task_slots: dict[str, Any] | None,
) -> dict[str, Any]:
    if not task_slots or not isinstance(task_slots, dict):
        return {}
    scope = str(task_slots.get("task_scope") or "").strip().lower()
    if scope not in _VALID_SLOT_SCOPES:
        return {}
    completed = task_slots.get("completed")
    if completed is not True and completed is not False:
        completed = None
    return {
        "task_scope": scope,
        "person_name": str(task_slots.get("person_name") or "").strip(),
        "tasklist_name": str(task_slots.get("tasklist_name") or "").strip(),
        "completed": completed,
    }


def resolve_task_query(
    message: str,
    *,
    slots: dict[str, Any] | None = None,
) -> tuple[str, bool | None, str | None, str | None]:
    """Return (kind, completed, person_name, tasklist_name). LLM slots win."""
    text = (message or "").strip()
    norm = _normalize_task_slots(slots)
    if norm:
        kind = str(norm["task_scope"])
        completed = norm["completed"]
        if completed is None:
            completed = parse_completed_filter(text)
        person = str(norm.get("person_name") or "").strip() or None
        list_name = str(norm.get("tasklist_name") or "").strip() or None
        if kind == "member" and not person:
            person = extract_member_name(text)
        if kind in {"tasklist", "board"} and not list_name:
            list_name = extract_tasklist_name(text)
        if kind == "member" and person and _is_self_name(person):
            kind = "mine"
            person = None
        return kind, completed, person, list_name

    completed = parse_completed_filter(text)
    kind = classify_query_kind(text)
    person = extract_member_name(text) if kind == "member" else None
    list_name = (
        extract_tasklist_name(text) if kind in {"tasklist", "board"} else None
    )
    return kind, completed, person, list_name


def classify_query_kind(message: str) -> str:
    """mine | member | list_all | tasklist | board | all_visible | clarify."""
    text = (message or "").strip()
    lower = text.lower()
    if any(m in lower for m in _LIST_ALL_MARKERS):
        return "list_all"
    if any(m in text for m in _ALL_VISIBLE_MARKERS):
        return "all_visible"
    if "看板" in text:
        return "board"
    if extract_tasklist_name(text):
        return "tasklist"
    # 先认「某人的任务」，避免「帮我看看辰子任务」被「我…任务」误判成 mine
    person = extract_member_name(text)
    if person and not _is_self_name(person):
        return "member"
    if _looks_like_self_tasks(text):
        return "mine"
    # 「有哪些任务」等未指明范围：先澄清，避免默认成「我负责的」
    return "clarify"


def _looks_like_self_tasks(text: str) -> bool:
    if any(m in text for m in _SELF_MARKERS):
        return True
    # 仅紧贴「我的/我最近的…任务」，不要匹配「帮我看看某人任务」
    if re.search(
        r"(?<![帮])我(?:最近|名下|负责)?的?(?:未完成|已完成)?(?:的)?"
        r"(?:任务|待办)(?:情况)?",
        text,
    ):
        return True
    return False


def extract_member_name(message: str) -> str | None:
    text = (message or "").strip()
    for pat in _MEMBER_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        name = m.group(1).strip(" ：:，,的")
        name = re.sub(
            r"^(?:帮我)?(?:查一下|查下|看看|查看|查询|看一下|看下)\s*",
            "",
            name,
        )
        if not name or name in {"谁", "哪位", "什么人", "下", "一下"}:
            continue
        if any(
            bad in name
            for bad in (
                "有哪些",
                "未完成",
                "已完成",
                "清单",
                "看板",
                "任务",
                "待办",
                "所有",
                "全部",
                "可见",
                "现在",
                "最近",
            )
        ):
            continue
        if len(name) > 20 or len(name) < 1:
            continue
        return name
    return None


def extract_tasklist_name(message: str) -> str | None:
    text = (message or "").strip()
    for pat in _TASKLIST_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        name = m.group(1).strip(" ：:，,")
        if name and name not in {"任务", "我的", "有哪些", "全部"}:
            return name
    return None


def parse_completed_filter(message: str) -> bool | None:
    """True=已完成, False=未完成, None=全部."""
    text = (message or "").strip().lower()
    if any(k in text for k in ("未完成", "没完成", "待办", "未做完", "还没做")):
        return False
    if any(k in text for k in ("已完成", "做完了", "完成的任务", "完成情况")):
        if "完成情况" in text and "已完成" not in text and "做完" not in text:
            return None
        return True
    return None


def _is_self_name(name: str) -> bool:
    n = (name or "").strip()
    return n in {"我", "自己", "本人"}


def _is_cancel(message: str) -> bool:
    text = (message or "").strip().lower()
    return any(m in text for m in CANCEL_MARKERS)


def _parse_pick_index(text: str, n: int) -> int | None:
    raw = (text or "").strip()
    m = re.match(r"^(\d+)\s*[.、)]?\s*$", raw)
    if not m:
        return None
    idx = int(m.group(1)) - 1
    if 0 <= idx < n:
        return idx
    return None


def _match_tasklists(items: list[dict[str, Any]], name_query: str) -> list[dict[str, Any]]:
    q = (name_query or "").strip().lower()
    if not q:
        return []
    exact: list[dict[str, Any]] = []
    soft: list[dict[str, Any]] = []
    for row in items:
        name = str(row.get("name") or "").strip()
        low = name.lower()
        if low == q:
            exact.append(row)
        elif q in low:
            soft.append(row)
    return exact or soft
