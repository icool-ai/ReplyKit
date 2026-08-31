"""HTTP adapter: ecommerce competitor agent (mp_agent) under ReplyKit auth."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from mp_agent.application.agent_service import (
    RUNS,
    discard_run,
    get_session_payload,
    new_run,
    new_session,
    run_session_message,
)
from mp_agent.application.session_store import ConcurrentRunError
from mp_agent.infrastructure.artifacts import ARTIFACTS_DIR
from mp_agent.presentation.http import (
    build_run_event_stream,
    run_session_message_with_cleanup,
    schedule_run_cleanup,
)
from src.api_response import ok

COMPETITOR_PREFIX = "/agents/ecommerce-competitor"


class CompetitorMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)


def register_competitor_routes(
    app: Any,
    *,
    require_auth: Callable[..., dict[str, Any]],
) -> None:
    """Mount competitor agent routes onto the main FastAPI app."""
    from mp_agent.application import agent_service as svc

    shared_store = svc.SESSION_STORE

    router = APIRouter(
        prefix=COMPETITOR_PREFIX,
        tags=["ecommerce-competitor"],
    )

    def _username(user: dict[str, Any]) -> str:
        return str(user.get("sub") or user.get("username") or "").strip()

    def _ensure_owner(session_id: str, username: str) -> None:
        try:
            shared_store.require_owner(session_id, username)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="无权访问该会话") from exc

    @router.post("/sessions")
    async def create_session(
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        username = _username(user)
        if not username:
            raise HTTPException(status_code=401, detail="无效用户")
        session = new_session(owner_username=username, session_store=shared_store)
        return ok({"session_id": session.session_id})

    @router.get("/sessions/{session_id}")
    async def get_session(
        session_id: str,
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        _ensure_owner(session_id, _username(user))
        try:
            payload = get_session_payload(session_id, session_store=shared_store)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        return ok(payload)

    @router.post("/sessions/{session_id}/messages")
    async def post_message(
        session_id: str,
        request: CompetitorMessageRequest,
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        _ensure_owner(session_id, _username(user))
        try:
            run = new_run(
                session_id,
                request.message.strip(),
                session_store=shared_store,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except ConcurrentRunError as exc:
            raise HTTPException(
                status_code=409, detail="Session already has an active run"
            ) from exc

        asyncio.create_task(
            run_session_message_with_cleanup(
                session_id,
                run,
                RUNS,
                run_session_message,
                schedule_run_cleanup,
            )
        )
        return ok({"session_id": session_id, "run_id": run.run_id})

    @router.get("/sessions/{session_id}/runs/{run_id}/stream")
    async def stream_run(
        session_id: str,
        run_id: str,
        user: dict[str, Any] = Depends(require_auth),
    ) -> StreamingResponse:
        _ensure_owner(session_id, _username(user))
        run = RUNS.get(run_id)
        if run is None or run.session_id != session_id:
            raise HTTPException(status_code=404, detail="Run not found")
        return StreamingResponse(
            build_run_event_stream(run_id, run, RUNS, discard_run),
            media_type="text/event-stream",
        )

    @router.get("/downloads/{filename}")
    async def download_file(
        filename: str,
        user: dict[str, Any] = Depends(require_auth),
    ) -> FileResponse:
        _ = user
        if Path(filename).name != filename:
            raise HTTPException(status_code=404, detail="File not found")
        path = Path(ARTIFACTS_DIR) / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        return FileResponse(path, filename=filename)

    app.include_router(router)
