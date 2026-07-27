"""Convert local asset file paths to HTTP URLs under /assets/."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, unquote, urlparse


def _encode_asset_rel(rel: Path) -> str:
    """Percent-encode each path segment (中文文件名可被浏览器 / Postman 打开)."""
    return "/".join(quote(part, safe="") for part in rel.parts)


def _strip_to_assets_rel(url_or_path: str) -> str | None:
    """If already an /assets/... link (absolute or relative), return the relative part."""
    text = url_or_path.strip()
    if not text:
        return None
    if text.startswith(("http://", "https://")):
        path = unquote(urlparse(text).path or "")
    else:
        path = unquote(text)
    marker = "/assets/"
    idx = path.find(marker)
    if idx < 0:
        if path.startswith("assets/"):
            return path[len("assets/") :]
        return None
    return path[idx + len(marker) :]


def paths_to_asset_urls(
    paths: list[str],
    assets_dir: Path,
    *,
    base_url: str = "",
) -> list[str]:
    """Convert local image paths under assets_dir to /assets/... URLs.

    ``base_url`` 若提供（如 ``http://127.0.0.1:8000``），则返回绝对 URL，
    便于 Postman / 跨域前端直接打开；否则返回相对路径 ``/assets/...``。
    """
    root = assets_dir.resolve()
    base = (base_url or "").rstrip("/")
    urls: list[str] = []
    seen: set[str] = set()

    for item in paths:
        raw = str(item).strip()
        if not raw:
            continue

        rel_text = _strip_to_assets_rel(raw)
        if rel_text is None:
            path = Path(raw)
            try:
                resolved = path.resolve()
                rel = resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if any(part == ".." for part in rel.parts):
                continue
            rel_text = rel.as_posix()

        rel_text = rel_text.lstrip("/")
        if not rel_text or ".." in Path(rel_text).parts:
            continue

        encoded = _encode_asset_rel(Path(rel_text))
        url = f"/assets/{encoded}"
        if base:
            url = f"{base}{url}"
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)

    return urls
