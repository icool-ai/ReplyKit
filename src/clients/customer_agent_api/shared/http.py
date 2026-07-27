"""Minimal JSON HTTP helper (stdlib only — no requests/httpx dependency)."""

from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen


class ApiError(Exception):
    """Raised when the API returns a non-2xx status or the request fails."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def join_url(base_url: str, path: str) -> str:
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    return urljoin(base, path.lstrip("/"))


def encode_path_segment(value: str) -> str:
    return quote(str(value), safe="")


def request_json(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 60.0,
) -> Any:
    """Send an HTTP request and parse a JSON response body.

    Supports unified envelope ``{code, message, data}``: returns ``data`` on
    success (2xx code); raises ``ApiError`` otherwise.
    """
    req_headers = {"Accept": "application/json"}
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)

    request = Request(url, data=data, headers=req_headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            payload = json.loads(raw)
            return _unwrap_envelope(payload, method=method, url=url)
    except HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")
        # Prefer business message from envelope when present.
        try:
            err_json = json.loads(err_body) if err_body else None
        except json.JSONDecodeError:
            err_json = None
        if isinstance(err_json, dict) and "message" in err_json:
            raise ApiError(
                str(err_json.get("message") or f"HTTP {exc.code}"),
                status=exc.code,
                body=err_body,
            ) from exc
        raise ApiError(
            f"{method.upper()} {url} failed with HTTP {exc.code}",
            status=exc.code,
            body=err_body,
        ) from exc
    except URLError as exc:
        raise ApiError(f"{method.upper()} {url} failed: {exc.reason}") from exc


def _is_success_code(code: Any) -> bool:
    try:
        n = int(code)
    except (TypeError, ValueError):
        return False
    return 200 <= n < 300


def _unwrap_envelope(payload: Any, *, method: str, url: str) -> Any:
    if not isinstance(payload, dict):
        return payload
    if "code" not in payload or "data" not in payload:
        return payload
    code = payload.get("code")
    if not _is_success_code(code):
        raise ApiError(
            str(payload.get("message") or f"API error code={code}"),
            status=int(code) if str(code).isdigit() else None,
            body=json.dumps(payload, ensure_ascii=False),
        )
    return payload.get("data")
