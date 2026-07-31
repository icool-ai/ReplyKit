"""Feishu Task Center query tools (user_access_token required)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any
from urllib.parse import urlencode

from src.channels.feishu import http_json

_TASK_URL = "https://open.feishu.cn/open-apis/task/v2/tasks"
_TASKLIST_URL = "https://open.feishu.cn/open-apis/task/v2/tasklists"
_SECTION_URL = "https://open.feishu.cn/open-apis/task/v2/sections"
_TZ_CN = timezone(timedelta(hours=8))


def list_my_tasks(
    user_access_token: str,
    *,
    completed: bool | None = None,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """GET /task/v2/tasks?type=my_tasks — returns raw task items."""
    token = _require_token(user_access_token)
    size = max(1, min(int(page_size), 100))
    params: dict[str, Any] = {"type": "my_tasks", "page_size": size}
    if completed is not None:
        params["completed"] = "true" if completed else "false"
    data = _get_json(token, f"{_TASK_URL}?{urlencode(params)}")
    return _items(data)


def search_tasks(
    user_access_token: str,
    *,
    assignee_open_ids: list[str] | None = None,
    completed: bool | None = None,
    query: str = "",
    page_size: int = 20,
) -> list[dict[str, Any]]:
    """POST /task/v2/tasks/search — visible tasks matching filters."""
    token = _require_token(user_access_token)
    size = max(1, min(int(page_size), 50))
    body: dict[str, Any] = {}
    q = (query or "").strip()
    if q:
        body["query"] = q[:50]
    filt: dict[str, Any] = {}
    ids = [x.strip() for x in (assignee_open_ids or []) if x and x.strip()]
    if ids:
        filt["assignee_ids"] = ids[:500]
    if completed is not None:
        filt["is_completed"] = bool(completed)
    if filt:
        body["filter"] = filt
    data = http_json(
        "POST",
        f"{_TASK_URL}/search?{urlencode({'page_size': size, 'user_id_type': 'open_id'})}",
        body=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if int(data.get("code") or 0) != 0:
        raise RuntimeError(f"飞书任务搜索失败: {data.get('msg') or data}")
    raw = (data.get("data") or {}).get("items") or []
    out: list[dict[str, Any]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        enriched = _search_item_to_task(token, row)
        if enriched:
            out.append(enriched)
    return out


def get_task(user_access_token: str, task_guid: str) -> dict[str, Any] | None:
    token = _require_token(user_access_token)
    guid = (task_guid or "").strip()
    if not guid:
        return None
    data = http_json(
        "GET",
        f"{_TASK_URL}/{guid}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20.0,
    )
    if int(data.get("code") or 0) != 0:
        return None
    task = (data.get("data") or {}).get("task")
    return task if isinstance(task, dict) else None


def list_tasklists(
    user_access_token: str,
    *,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """GET /task/v2/tasklists — tasklists the caller can read."""
    token = _require_token(user_access_token)
    size = max(1, min(int(page_size), 100))
    data = _get_json(token, f"{_TASKLIST_URL}?{urlencode({'page_size': size})}")
    return _items(data)


def list_tasklist_tasks(
    user_access_token: str,
    tasklist_guid: str,
    *,
    completed: bool | None = None,
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """GET /task/v2/tasklists/{guid}/tasks."""
    token = _require_token(user_access_token)
    guid = (tasklist_guid or "").strip()
    if not guid:
        raise ValueError("tasklist_guid 不能为空")
    size = max(1, min(int(page_size), 100))
    params: dict[str, Any] = {"page_size": size}
    if completed is not None:
        params["completed"] = "true" if completed else "false"
    url = f"{_TASKLIST_URL}/{guid}/tasks?{urlencode(params)}"
    data = _get_json(token, url)
    return _items(data)


def list_sections(
    user_access_token: str,
    *,
    resource_type: str = "tasklist",
    resource_id: str = "",
    page_size: int = 50,
) -> list[dict[str, Any]]:
    """GET /task/v2/sections — sections (看板列/自定义分组)."""
    token = _require_token(user_access_token)
    size = max(1, min(int(page_size), 100))
    params: dict[str, Any] = {
        "page_size": size,
        "resource_type": resource_type.strip() or "tasklist",
    }
    rid = (resource_id or "").strip()
    if rid:
        params["resource_id"] = rid
    data = _get_json(token, f"{_SECTION_URL}?{urlencode(params)}")
    return _items(data)


def filter_tasks_by_assignee(
    items: list[dict[str, Any]],
    open_id: str,
) -> list[dict[str, Any]]:
    oid = (open_id or "").strip()
    if not oid:
        return []
    out: list[dict[str, Any]] = []
    for task in items:
        members = task.get("members") or []
        if not isinstance(members, list):
            continue
        for m in members:
            if not isinstance(m, dict):
                continue
            if str(m.get("id") or "").strip() != oid:
                continue
            role = str(m.get("role") or "").strip().lower()
            if role in {"", "assignee"}:
                out.append(task)
                break
    return out


def format_task_list(
    items: list[dict[str, Any]],
    *,
    completed_filter: bool | None = None,
    owner_label: str = "你",
    assignee_scoped: bool = True,
) -> str:
    """Short text summary: index, title, status, due."""
    if completed_filter is True:
        status_part = "已完成"
    elif completed_filter is False:
        status_part = "未完成"
    elif assignee_scoped:
        status_part = "负责的"
    else:
        status_part = ""

    if not items:
        if status_part == "负责的":
            return f"暂无{owner_label}负责的任务（仅含你有权限看到的）。"
        if status_part:
            return f"暂无{owner_label}的{status_part}任务（仅含你有权限看到的）。"
        return f"暂无可见任务（仅含你有权限看到的）。"

    if status_part == "负责的":
        lines = [f"{owner_label}负责的任务（共 {len(items)} 条，仅含可见范围）："]
    elif status_part:
        lines = [
            f"{owner_label}的{status_part}任务（共 {len(items)} 条，仅含可见范围）："
        ]
    else:
        lines = [f"你可见的任务（共 {len(items)} 条，含他人任务若已共享）："]
    for i, task in enumerate(items, 1):
        lines.append(_format_task_line(i, task))
    return "\n".join(lines)


def format_tasklist_names(items: list[dict[str, Any]]) -> str:
    if not items:
        return "你当前没有可读的任务清单。"
    lines = [f"你可读的任务清单（共 {len(items)} 个）："]
    for i, row in enumerate(items, 1):
        name = str(row.get("name") or "").strip() or "（未命名）"
        lines.append(f"{i}. {name}")
    lines.append("可再说「看看【清单名】清单」或「【清单名】看板」。")
    return "\n".join(lines)


def format_board(
    *,
    tasklist_name: str,
    sections: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    completed_filter: bool | None = None,
) -> str:
    name = (tasklist_name or "").strip() or "清单"
    filtered = tasks
    if completed_filter is True:
        filtered = [t for t in tasks if _is_completed(t)]
    elif completed_filter is False:
        filtered = [t for t in tasks if not _is_completed(t)]

    if not sections:
        return format_task_list(
            filtered,
            completed_filter=completed_filter,
            owner_label=f"「{name}」清单中的",
        )

    by_section: dict[str, list[dict[str, Any]]] = {
        str(s.get("guid") or ""): [] for s in sections if isinstance(s, dict)
    }
    ungrouped: list[dict[str, Any]] = []
    for task in filtered:
        sec = _task_section_guid(task)
        if sec and sec in by_section:
            by_section[sec].append(task)
        else:
            ungrouped.append(task)

    lines = [f"「{name}」看板/分组（共 {len(filtered)} 条任务）："]
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        guid = str(sec.get("guid") or "")
        sec_name = str(sec.get("name") or "").strip() or "未命名分组"
        bucket = by_section.get(guid) or []
        lines.append(f"\n【{sec_name}】（{len(bucket)}）")
        if not bucket:
            lines.append("  （空）")
            continue
        for i, task in enumerate(bucket[:15], 1):
            lines.append("  " + _format_task_line(i, task).replace("\n   ", "｜"))
        if len(bucket) > 15:
            lines.append(f"  …另有 {len(bucket) - 15} 条")
    if ungrouped:
        lines.append(f"\n【未分组】（{len(ungrouped)}）")
        for i, task in enumerate(ungrouped[:10], 1):
            lines.append("  " + _format_task_line(i, task).replace("\n   ", "｜"))
    return "\n".join(lines)


def _format_task_line(index: int, task: dict[str, Any]) -> str:
    title = str(task.get("summary") or "").strip() or "（无标题）"
    status = _status_label(task)
    due = _format_due(task.get("due"))
    detail = f"状态：{status}"
    if due:
        detail += f"｜截止：{due}"
    return f"{index}. {title}\n   {detail}"


def _task_section_guid(task: dict[str, Any]) -> str:
    lists = task.get("tasklists") or []
    if isinstance(lists, list):
        for row in lists:
            if isinstance(row, dict):
                g = str(row.get("section_guid") or "").strip()
                if g:
                    return g
    return str(task.get("section_guid") or "").strip()


def _search_item_to_task(token: str, row: dict[str, Any]) -> dict[str, Any] | None:
    guid = str(row.get("id") or "").strip()
    meta = row.get("meta_data") if isinstance(row.get("meta_data"), dict) else {}
    app_link = str((meta or {}).get("app_link") or "")
    if "guid=" in app_link:
        part = app_link.split("guid=", 1)[1]
        guid = part.split("&", 1)[0].strip() or guid
    if guid and "-" in guid:
        detail = get_task(token, guid)
        if detail:
            return detail
    # Fallback minimal shape from search snippet.
    display = str(row.get("display_info") or "").replace("<h>", "").replace("</h>", "")
    title = display.strip() or str((meta or {}).get("description") or "").strip()
    if not title:
        return None
    return {"guid": guid, "summary": title, "status": "todo"}


def _require_token(user_access_token: str) -> str:
    token = (user_access_token or "").strip()
    if not token:
        raise ValueError("user_access_token 不能为空")
    return token


def _get_json(token: str, url: str) -> dict[str, Any]:
    data = http_json(
        "GET",
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if int(data.get("code") or 0) != 0:
        raise RuntimeError(f"飞书接口失败: {data.get('msg') or data}")
    return data


def _items(data: dict[str, Any]) -> list[dict[str, Any]]:
    items = (data.get("data") or {}).get("items") or []
    return [i for i in items if isinstance(i, dict)]


def _is_completed(task: dict[str, Any]) -> bool:
    status = str(task.get("status") or "").strip().lower()
    if status == "done":
        return True
    completed_at = str(task.get("completed_at") or "").strip()
    return bool(completed_at) and completed_at != "0"


def _status_label(task: dict[str, Any]) -> str:
    return "已完成" if _is_completed(task) else "未完成"


def _format_due(due: Any) -> str:
    if not isinstance(due, dict):
        return ""
    ts_raw = due.get("timestamp")
    if ts_raw is None or ts_raw == "":
        return ""
    try:
        ms = int(ts_raw)
    except (TypeError, ValueError):
        return ""
    dt = datetime.fromtimestamp(ms / 1000.0, tz=_TZ_CN)
    if due.get("is_all_day"):
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M")
