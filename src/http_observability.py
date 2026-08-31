from __future__ import annotations

import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from starlette.responses import Response

DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(level: str | None = None) -> int:
    """Set a sensible default logger without clobbering existing handlers."""

    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    resolved_level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        logging.basicConfig(level=resolved_level, format=DEFAULT_LOG_FORMAT)

    root_logger.setLevel(resolved_level)
    logging.getLogger("replykit").setLevel(resolved_level)
    logging.getLogger("mp_agent").setLevel(resolved_level)
    return resolved_level


def get_request_id(request: Request) -> str:
    request_id = getattr(request.state, "request_id", "")
    if request_id:
        return request_id

    header_value = request.headers.get("X-Request-ID", "").strip()
    request_id = (header_value or uuid.uuid4().hex)[:32]
    request.state.request_id = request_id
    return request_id


def register_request_logging_middleware(
    app: FastAPI, *, logger_name: str = "replykit.http"
) -> None:
    logger = logging.getLogger(logger_name)

    @app.middleware("http")
    async def request_logging_middleware(
        request: Request, call_next
    ) -> Response:
        request_id = get_request_id(request)
        started_at = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            response.headers.setdefault("X-Request-ID", request_id)
            return response
        finally:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            status_code = response.status_code if response is not None else 500
            logger.info(
                "%s %s -> %s (%sms) request_id=%s",
                request.method,
                request.url.path,
                status_code,
                duration_ms,
                request_id,
            )
