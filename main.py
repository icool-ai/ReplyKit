"""Entry point for the customer service agent API."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="ReplyKit API")
    parser.add_argument("--host", default=None, help="监听地址（默认 API_HOST 或 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 API_PORT 或 8000）")
    args = parser.parse_args()

    import uvicorn

    from src.config import get_settings
    from src.http_observability import configure_logging

    configure_logging()
    get_settings()  # 启动前校验 API Key 等配置
    host = args.host or os.getenv("API_HOST", "127.0.0.1")
    port = args.port or int(os.getenv("API_PORT", "8000"))
    uvicorn.run("src.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
