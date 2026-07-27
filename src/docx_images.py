"""Extract images from Word docs and bind them to nearby text sections."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Iterator

from docx import Document as DocxFile
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph
from langchain_core.documents import Document

CONTENT_TYPE_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/x-emf": ".emf",
    "image/x-wmf": ".wmf",
}


def _iter_block_items(document: DocxDocument) -> Iterator[Paragraph | Table]:
    body = document.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, document)
        elif isinstance(child, CT_Tbl):
            yield Table(child, document)


def _looks_like_heading(text: str) -> bool:
    """Heuristic: short title-like lines start a new section."""
    text = text.strip()
    if not text or len(text) > 24:
        return False
    if re.search(r"[。！？；;]", text):
        return False
    if text.count("，") >= 2 or text.count(",") >= 2:
        return False
    return True


def _table_to_text(table: Table) -> str:
    rows: list[str] = []
    for row in table.rows:
        cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
        if cells:
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _save_image_part(part, out_dir: Path, stem: str, index: int) -> Path | None:
    content_type = getattr(part, "content_type", "") or ""
    ext = CONTENT_TYPE_EXT.get(content_type)
    if not ext:
        # Fallback from partname, e.g. /word/media/image1.png
        name = Path(str(getattr(part, "partname", "image.bin"))).suffix.lower()
        ext = name if name in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"} else ".bin"
        if ext == ".jpeg":
            ext = ".jpg"
    if ext in {".emf", ".wmf", ".bin"}:
        # Skip formats browsers usually cannot preview.
        return None

    blob = part.blob
    digest = hashlib.md5(blob).hexdigest()[:10]
    filename = f"{stem}_{index:03d}_{digest}{ext}"
    target = out_dir / filename
    if not target.exists():
        target.write_bytes(blob)
    return target


def _images_in_paragraph(
    paragraph: Paragraph, document: DocxDocument, out_dir: Path, stem: str, counter: list[int]
) -> list[str]:
    paths: list[str] = []
    # a:blip/@r:embed points to the image relationship id.
    for blip in paragraph._element.xpath(".//a:blip"):
        embed = blip.get(qn("r:embed"))
        if not embed:
            continue
        try:
            part = document.part.related_parts[embed]
        except KeyError:
            continue
        counter[0] += 1
        saved = _save_image_part(part, out_dir, stem, counter[0])
        if saved is not None:
            paths.append(str(saved.resolve()))
    return paths


def load_docx_with_images(path: Path, assets_dir: Path) -> list[Document]:
    """Load a .docx as section Documents, each optionally carrying image paths."""
    path = path.resolve()
    doc = DocxFile(str(path))
    stem = re.sub(r"[^\w\u4e00-\u9fff\-]+", "_", path.stem).strip("_") or "doc"
    out_dir = assets_dir / stem
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sections: list[dict] = []
    buf_text: list[str] = []
    buf_images: list[str] = []
    counter = [0]

    def flush() -> None:
        nonlocal buf_text, buf_images
        if not buf_text and not buf_images:
            return
        if not buf_text and sections:
            # Orphan images attach to the previous section.
            sections[-1]["images"].extend(buf_images)
        else:
            text = "\n".join(buf_text).strip()
            if text or buf_images:
                sections.append({"text": text, "images": list(buf_images)})
        buf_text = []
        buf_images = []

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            images = _images_in_paragraph(block, doc, out_dir, stem, counter)
            text = block.text.strip()
            if text and len(text) > 1:
                if _looks_like_heading(text) and (buf_text or buf_images):
                    flush()
                buf_text.append(text)
            buf_images.extend(images)
        elif isinstance(block, Table):
            table_text = _table_to_text(block)
            if table_text:
                buf_text.append(table_text)

    flush()

    documents: list[Document] = []
    for section in sections:
        text = section["text"].strip()
        images = section["images"]
        if not text and not images:
            continue
        # Indexable text: prefer real prose; fall back so image-only sections still embed.
        page_content = text or f"【配图说明】来自文档 {path.name}"
        documents.append(
            Document(
                page_content=page_content,
                metadata={
                    "source": str(path),
                    "images": images,
                },
            )
        )

    return documents
