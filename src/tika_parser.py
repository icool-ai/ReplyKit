"""Apache Tika 统一文档解析封装 + 优雅降级。

对外暴露两个高层 API（与 Redis 同样采用「失败即降级」原则）：

- ``extract_text_from_bytes(data, filename_hint, settings)``
    适合 FastAPI UploadFile / FAQ 导入等字节流场景

- ``extract_text_from_path(path, settings)``
    适合知识库 ``docs/`` 目录下磁盘文件扫描场景

当 ``settings.tika_enabled=False``、或 Tika Server 连接失败、
或 ``tika`` SDK 未安装时，两个函数都会**返回空字符串**而不是抛出异常，
由上层（knowledge.py / faq_import.py）按空内容静默跳过。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from src.config import Settings

logger = logging.getLogger(__name__)

_client: Any | None = None
_client_checked: bool = False


def _get_client(settings: Settings) -> Any | None:
    """Lazy + 缓存 Tika Server 句柄；失败统一返回 None。"""
    global _client, _client_checked

    if not settings.tika_enabled:
        return None

    if _client_checked and _client is not None:
        return _client

    if _client_checked:
        return None

    try:
        from tika import parser as _tika_parser  # noqa: WPS433

        import tika as _tika_pkg

        # 强制使用用户指定的 Tika Server 地址（禁止 SDK 后台起 JVM 进程）
        server_url = settings.tika_url.rstrip("/")
        if not server_url.endswith("/tika"):
            server_url = f"{server_url}/tika"
        base = server_url.removesuffix("/tika")
        _tika_pkg.TikaServerEndpoint = base
        _tika_pkg.TikaClientOnly = True
        _client = _tika_parser
    except Exception as exc:
        logger.warning("Tika SDK 未安装或初始化失败，解析将按空文本降级：%s", exc)
        _client = None
    finally:
        _client_checked = True

    return _client


def extract_text_from_bytes(
    data: bytes,
    filename_hint: str,
    settings: Settings,
) -> tuple[str, dict[str, Any]]:
    """用 Tika Server 解析任意字节流。

    Returns:
      (text: str, metadata: dict)。失败 / 禁用 / 空内容时返回 ("", {})。
    """
    if not data:
        return "", {}
    client = _get_client(settings)
    if client is None:
        return "", {}
    try:
        parsed = client.from_buffer(
            data,
            serverEndpoint=_server_base(settings),
            requestOptions={"timeout": settings.tika_timeout_sec},
        )
    except Exception as exc:
        logger.warning(
            "Tika 解析字节流失败（name=%s，%s），按空文本降级",
            filename_hint,
            exc,
        )
        return "", {}
    text = (parsed.get("content") or "").strip()
    meta = dict(parsed.get("metadata") or {})
    return text, meta


def extract_text_from_path(
    path: Path,
    settings: Settings,
) -> tuple[str, dict[str, Any]]:
    """用 Tika Server 解析本地文件。

    Returns:
      (text: str, metadata: dict)。失败 / 禁用 / 空文件时返回 ("", {})。
    """
    resolved = path.resolve()
    if not resolved.exists() or not resolved.is_file():
        return "", {}
    client = _get_client(settings)
    if client is None:
        return "", {}
    try:
        parsed = client.from_file(
            str(resolved),
            serverEndpoint=_server_base(settings),
            requestOptions={"timeout": settings.tika_timeout_sec},
        )
    except Exception as exc:
        logger.warning(
            "Tika 解析文件失败（%s，%s），按空文本降级", resolved, exc,
        )
        return "", {}
    text = (parsed.get("content") or "").strip()
    meta = dict(parsed.get("metadata") or {})
    return text, meta


def _server_base(settings: Settings) -> str:
    """tika-python 需要的是基础 URL（不含 /tika 后缀）。"""
    url = (settings.tika_url or "").rstrip("/")
    if url.endswith("/tika"):
        return url[: -len("/tika")]
    return url
