"""Unified RESTful API envelope: ``{code, message, data}``.

``code`` mirrors HTTP status (200 success, 4xx/5xx errors).
"""

from __future__ import annotations

import logging
from typing import Any, Generic, TypeVar

from fastapi.encoders import jsonable_encoder
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.http_observability import get_request_id

T = TypeVar("T")
logger = logging.getLogger("replykit.http")


class ApiResponse(BaseModel, Generic[T]):
    """Standard JSON body for all JSON API endpoints."""

    code: int = Field(
        200,
        description="与 HTTP 状态码一致：200 成功，4xx 客户端错误，500 服务端错误",
    )
    message: str = Field("ok", description="提示信息")
    data: T | None = Field(default=None, description="业务数据；失败时多为 null")


def ok(data: Any = None, *, message: str = "ok", code: int = 200) -> dict[str, Any]:
    """Success envelope. Default code=200."""
    return {"code": code, "message": message, "data": data}


def fail(
    *,
    code: int,
    message: str,
    data: Any = None,
    http_status: int | None = None,
) -> JSONResponse:
    """Error envelope. ``code`` should be an HTTP status (e.g. 400/401/422/500)."""
    status = http_status if http_status is not None else code
    if status < 400:
        status = 400
    return JSONResponse(
        status_code=status,
        content={"code": code, "message": message, "data": data},
    )


def register_exception_handlers(app: Any) -> None:
    """HTTP / validation / uncaught errors all use the same envelope."""

    def _response_headers(
        request: Request, extra_headers: dict[str, str] | None = None
    ) -> dict[str, str]:
        headers = dict(extra_headers or {})
        headers.setdefault("X-Request-ID", get_request_id(request))
        return headers

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, (list, dict)):
            message = "请求失败"
            data: Any = detail
        else:
            message = str(detail) if detail is not None else "请求失败"
            data = None
        headers = getattr(exc, "headers", None) or None
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "message": message, "data": data},
            headers=_response_headers(request, headers),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = jsonable_encoder(
            exc.errors(),
            custom_encoder={Exception: str, ValueError: str},
        )
        logger.warning(
            "validation failed %s %s request_id=%s errors=%s",
            request.method,
            request.url.path,
            get_request_id(request),
            errors,
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "message": "参数校验失败",
                "data": errors,
            },
            headers=_response_headers(request),
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = get_request_id(request)
        logger.exception(
            "unhandled error %s %s request_id=%s",
            request.method,
            request.url.path,
            request_id,
        )
        # Avoid leaking stack traces in the message; keep a short detail in data.
        return JSONResponse(
            status_code=500,
            content={
                "code": 500,
                "message": "服务器内部错误",
                "data": {"detail": f"{type(exc).__name__}: {exc}"},
            },
            headers=_response_headers(request),
        )
