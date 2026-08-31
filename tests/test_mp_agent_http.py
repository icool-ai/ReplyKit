from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import httpx

from mp_agent.application.session_store import ConcurrentRunError
from mp_agent.presentation.http import create_app


async def noop_run_session_message(_session_id: str, _run_id: str, _queue) -> None:
    return None


class MpAgentHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_session_returns_restful_envelope(self) -> None:
        app = create_app(
            frontend_dir=Path("d:/学习/agent/non-existent-frontend"),
            runs={},
            new_session_fn=lambda: SimpleNamespace(session_id="session-1"),
            get_session_payload_fn=lambda _session_id: {},
            new_run_fn=lambda _session_id, _message: None,
            run_session_message_fn=noop_run_session_message,
            discard_run_fn=lambda *_args, **_kwargs: None,
            cleanup_scheduler=lambda *_args, **_kwargs: None,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post("/api/sessions")

        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            response.json(),
            {
                "code": 201,
                "message": "ok",
                "data": {"session_id": "session-1"},
            },
        )
        self.assertTrue(response.headers["X-Request-ID"])

    async def test_blank_message_fails_validation(self) -> None:
        app = create_app(
            frontend_dir=Path("d:/学习/agent/non-existent-frontend"),
            runs={},
            new_session_fn=lambda: SimpleNamespace(session_id="session-1"),
            get_session_payload_fn=lambda _session_id: {},
            new_run_fn=lambda _session_id, _message: None,
            run_session_message_fn=noop_run_session_message,
            discard_run_fn=lambda *_args, **_kwargs: None,
            cleanup_scheduler=lambda *_args, **_kwargs: None,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/sessions/session-1/messages",
                json={"message": "   "},
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], 422)
        self.assertEqual(response.json()["message"], "参数校验失败")
        self.assertTrue(response.headers["X-Request-ID"])

    async def test_missing_session_returns_404_envelope(self) -> None:
        def missing_session(_session_id: str) -> dict:
            raise KeyError("missing")

        app = create_app(
            frontend_dir=Path("d:/学习/agent/non-existent-frontend"),
            runs={},
            new_session_fn=lambda: SimpleNamespace(session_id="session-1"),
            get_session_payload_fn=missing_session,
            new_run_fn=lambda _session_id, _message: None,
            run_session_message_fn=noop_run_session_message,
            discard_run_fn=lambda *_args, **_kwargs: None,
            cleanup_scheduler=lambda *_args, **_kwargs: None,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/sessions/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(),
            {"code": 404, "message": "Session not found", "data": None},
        )

    async def test_concurrent_run_returns_409_envelope(self) -> None:
        def concurrent_run(_session_id: str, _message: str):
            raise ConcurrentRunError("busy")

        app = create_app(
            frontend_dir=Path("d:/学习/agent/non-existent-frontend"),
            runs={},
            new_session_fn=lambda: SimpleNamespace(session_id="session-1"),
            get_session_payload_fn=lambda _session_id: {},
            new_run_fn=concurrent_run,
            run_session_message_fn=noop_run_session_message,
            discard_run_fn=lambda *_args, **_kwargs: None,
            cleanup_scheduler=lambda *_args, **_kwargs: None,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/sessions/session-1/messages",
                json={"message": "hello"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json(),
            {
                "code": 409,
                "message": "Session already has an active run",
                "data": None,
            },
        )

    async def test_download_returns_404_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = create_app(
                frontend_dir=Path("d:/学习/agent/non-existent-frontend"),
                artifacts_dir=Path(tmpdir),
                runs={},
                new_session_fn=lambda: SimpleNamespace(session_id="session-1"),
                get_session_payload_fn=lambda _session_id: {},
                new_run_fn=lambda _session_id, _message: None,
                run_session_message_fn=noop_run_session_message,
                discard_run_fn=lambda *_args, **_kwargs: None,
                cleanup_scheduler=lambda *_args, **_kwargs: None,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                response = await client.get("/api/download/secret.txt")

            self.assertEqual(response.status_code, 404)
            self.assertEqual(
                response.json(),
                {"code": 404, "message": "File not found", "data": None},
            )


if __name__ == "__main__":
    unittest.main()
