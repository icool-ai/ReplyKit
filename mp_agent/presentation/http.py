from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

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
from src.api_response import ApiResponse, ok, register_exception_handlers
from src.http_observability import (
    configure_logging,
    register_request_logging_middleware,
)


BASE_DIR = Path(__file__).resolve().parents[2]
FRONTEND_DIR = BASE_DIR / "frontend"
TERMINAL_EVENT_TYPES = {"done"}
logger = logging.getLogger("mp_agent.http")


class MessageRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户消息，不能为空")

    @model_validator(mode="after")
    def validate_message(self) -> "MessageRequest":
        self.message = self.message.strip()
        if not self.message:
            raise ValueError("message 不能为空")
        return self


class SessionCreatedData(BaseModel):
    session_id: str


class SessionMessageItem(BaseModel):
    role: str
    content: str


class SessionSlotsData(BaseModel):
    platform: str | None = None
    brand: str | None = None
    count: int | None = None


class SessionPayloadData(BaseModel):
    session_id: str
    messages: list[SessionMessageItem]
    slots: SessionSlotsData
    active_run_id: str | None = None


class RunCreatedData(BaseModel):
    session_id: str
    run_id: str


class SafeStaticFiles(StaticFiles):
    async def check_config(self) -> None:
        if self.directory and not Path(self.directory).exists():
            return
        await super().check_config()

    async def get_response(self, path: str, scope):
        if self.directory and not Path(self.directory).exists():
            return Response(status_code=404)
        return await super().get_response(path, scope)


def build_run_event_stream(run_id: str, run, runs: dict, discard_run_fn=discard_run):
    async def event_stream():
        terminal_reached = False
        try:
            while True:
                payload = await run.queue.get()
                terminal_reached = payload.get("type") in TERMINAL_EVENT_TYPES
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                if terminal_reached:
                    break
        finally:
            if terminal_reached:
                discard_run_fn(run_id, runs=runs)

    return event_stream()


def schedule_run_cleanup(run_id: str, runs: dict, delay_seconds: float = 60.0, discard_run_fn=discard_run) -> None:
    async def cleanup():
        await asyncio.sleep(delay_seconds)
        discard_run_fn(run_id, runs=runs)

    asyncio.create_task(cleanup())


async def run_session_message_with_cleanup(
    session_id: str,
    run,
    runs: dict,
    run_session_message_fn,
    cleanup_scheduler,
) -> None:
    try:
        await run_session_message_fn(session_id, run.run_id, run.queue)
    except Exception:
        logger.exception(
            "run_session_message task failed session_id=%s run_id=%s",
            session_id,
            run.run_id,
        )
        raise
    finally:
        cleanup_scheduler(run.run_id, runs)


def create_app(
    *,
    frontend_dir: Path = FRONTEND_DIR,
    artifacts_dir: Path = ARTIFACTS_DIR,
    runs: dict = RUNS,
    new_session_fn=new_session,
    get_session_payload_fn=get_session_payload,
    new_run_fn=new_run,
    run_session_message_fn=run_session_message,
    discard_run_fn=discard_run,
    cleanup_scheduler=schedule_run_cleanup,
):
    configure_logging()
    app = FastAPI(
        title="Ecommerce Competitor Agent API",
        version="0.1.0",
        description=(
            "竞品分析子系统 API。"
            "JSON 响应统一为 {code, message, data}；"
            "POST /api/sessions 创建会话；"
            "POST /api/sessions/{session_id}/messages 创建一次运行；"
            "GET /api/sessions/{session_id}/runs/{run_id}/stream 使用 SSE 拉取事件流。"
        ),
    )
    register_request_logging_middleware(app, logger_name="mp_agent.http")
    register_exception_handlers(app)
    app.mount(
        "/static",
        SafeStaticFiles(directory=str(frontend_dir), html=True, check_dir=False),
        name="static",
    )

    @app.get("/")
    async def index():
        index_file = frontend_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return HTMLResponse("<html><body><h1>竞品分析</h1></body></html>")

    @app.post(
        "/api/sessions",
        status_code=201,
        response_model=ApiResponse[SessionCreatedData],
    )
    async def create_session_route():
        session = new_session_fn()
        return ok(SessionCreatedData(session_id=session.session_id).model_dump(), code=201)

    @app.get(
        "/api/sessions/{session_id}",
        response_model=ApiResponse[SessionPayloadData],
    )
    async def get_session_route(session_id: str):
        try:
            payload = SessionPayloadData(**get_session_payload_fn(session_id))
            return ok(payload.model_dump())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc

    @app.post(
        "/api/sessions/{session_id}/messages",
        status_code=201,
        response_model=ApiResponse[RunCreatedData],
    )
    async def post_message(session_id: str, request: MessageRequest):
        try:
            run = new_run_fn(session_id, request.message)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Session not found") from exc
        except ConcurrentRunError as exc:
            raise HTTPException(status_code=409, detail="Session already has an active run") from exc

        asyncio.create_task(
            run_session_message_with_cleanup(
                session_id,
                run,
                runs,
                run_session_message_fn,
                cleanup_scheduler,
            )
        )
        return ok(
            RunCreatedData(session_id=session_id, run_id=run.run_id).model_dump(),
            code=201,
        )

    @app.get("/api/sessions/{session_id}/runs/{run_id}/stream")
    async def stream_run(session_id: str, run_id: str):
        run = runs.get(run_id)
        if run is None or run.session_id != session_id:
            raise HTTPException(status_code=404, detail="Run not found")
        return StreamingResponse(
            build_run_event_stream(run_id, run, runs, discard_run_fn),
            media_type="text/event-stream",
        )

    @app.get("/api/download/{filename}")
    async def download_file(filename: str):
        if Path(filename).name != filename:
            raise HTTPException(status_code=404, detail="File not found")

        path = artifacts_dir / filename
        if not path.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        return FileResponse(path, filename=filename)

    return app


app = create_app()
