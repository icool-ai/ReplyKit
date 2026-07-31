"""Feishu contact helpers: resolve display name → open_id."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from src.channels.feishu import http_json

_SEARCH_USER_URL = "https://open.feishu.cn/open-apis/search/v1/user"


@dataclass(frozen=True)
class FeishuUserHit:
    open_id: str
    name: str


def search_users_by_name(
    user_access_token: str,
    query: str,
    *,
    page_size: int = 20,
) -> list[FeishuUserHit]:
    """GET /search/v1/user — match users by name keyword (needs contact:user:search)."""
    token = (user_access_token or "").strip()
    q = (query or "").strip()
    if not token:
        raise ValueError("user_access_token 不能为空")
    if not q:
        return []
    params = {"query": q, "page_size": max(1, min(int(page_size), 50))}
    data = http_json(
        "GET",
        f"{_SEARCH_USER_URL}?{urlencode(params)}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20.0,
    )
    if int(data.get("code") or 0) != 0:
        raise RuntimeError(f"飞书搜人失败: {data.get('msg') or data}")
    users = (data.get("data") or {}).get("users") or []
    out: list[FeishuUserHit] = []
    seen: set[str] = set()
    for row in users:
        if not isinstance(row, dict):
            continue
        oid = str(row.get("open_id") or "").strip()
        name = str(row.get("name") or "").strip() or oid
        if not oid or oid in seen:
            continue
        seen.add(oid)
        out.append(FeishuUserHit(open_id=oid, name=name))
    return out
