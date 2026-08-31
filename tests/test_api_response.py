from __future__ import annotations

import unittest

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.api_response import register_exception_handlers
from src.http_observability import register_request_logging_middleware


class EchoRequest(BaseModel):
    message: str = Field(..., min_length=1)


def build_app() -> FastAPI:
    app = FastAPI()
    register_request_logging_middleware(app, logger_name="replykit.test")
    register_exception_handlers(app)

    @app.post("/echo")
    async def echo(request: EchoRequest):
        return {"message": request.message}

    @app.get("/missing")
    async def missing():
        raise HTTPException(status_code=404, detail="not found")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    return app


class ApiResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_exception_uses_unified_envelope(self) -> None:
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.get("/missing")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json(),
            {"code": 404, "message": "not found", "data": None},
        )
        self.assertTrue(response.headers["X-Request-ID"])

    async def test_validation_error_uses_unified_envelope(self) -> None:
        transport = httpx.ASGITransport(app=build_app())
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            response = await client.post("/echo", json={"message": ""})

        self.assertEqual(response.status_code, 422)
        payload = response.json()
        self.assertEqual(payload["code"], 422)
        self.assertEqual(payload["message"], "参数校验失败")
        self.assertTrue(payload["data"])
        self.assertTrue(response.headers["X-Request-ID"])

    async def test_unhandled_error_is_logged_and_wrapped(self) -> None:
        transport = httpx.ASGITransport(app=build_app(), raise_app_exceptions=False)
        with self.assertLogs("replykit.http", level="ERROR") as captured:
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                response = await client.get("/boom")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["code"], 500)
        self.assertEqual(response.json()["message"], "服务器内部错误")
        self.assertIn("RuntimeError: boom", response.json()["data"]["detail"])
        self.assertTrue(any("unhandled error GET /boom" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
