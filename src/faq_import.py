"""FAQ import parsers + downloadable templates (json/csv/txt/xls/xlsx)."""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# 表格模板使用的中文表头（CSV / Excel）；导入时由 _norm_header 映射回内部字段名
TABLE_HEADERS_ZH: list[str] = ["编号", "分类", "标准问", "答案", "相似问"]

FIELD_DOCS: list[dict[str, str]] = [
    {
        "field": "id",
        "zh": "编号",
        "required": "否",
        "meaning": (
            "可留空。当前导入不会使用该列，系统会为每条 FAQ 自动生成编号；"
            "模板里填写仅作示例参考。"
        ),
    },
    {
        "field": "category",
        "zh": "分类",
        "required": "否",
        "meaning": "业务分类，便于管理，例如「订单物流」「支付」。",
    },
    {
        "field": "question",
        "zh": "标准问",
        "required": "是",
        "meaning": "该条 FAQ 的主问题表述（用户最常问的那一句）。",
    },
    {
        "field": "answer",
        "zh": "答案",
        "required": "是",
        "meaning": "标准答案正文，作为客服回复依据。",
    },
    {
        "field": "similar",
        "zh": "相似问",
        "required": "否",
        "meaning": (
            "用户换一种说法时的相似问。"
            "JSON 中为字符串数组；表格中多个相似问用 | 分隔，例如：收货地址能改吗|订单地址怎么改"
        ),
    },
]

SAMPLE_ENTRIES: list[dict[str, Any]] = [
    {
        "id": "sample_1",
        "category": "订单物流",
        "question": "如何修改收货地址？",
        "similar": ["收货地址能改吗", "订单地址怎么改"],
        "answer": "未发货订单可在「我的订单」中修改；已发货请联系客服或拒收后重新下单。",
    },
    {
        "id": "sample_2",
        "category": "支付",
        "question": "支持哪些支付方式？",
        "similar": ["可以怎么付款", "支持微信支付吗"],
        "answer": "支持微信、支付宝、银联及企业对公转账（企业版）。",
    },
]

SUPPORTED_FORMATS = ("json", "csv", "txt", "xls", "xlsx")

_FORMAT_META: dict[str, dict[str, str]] = {
    "json": {
        "filename": "faq_template.json",
        "description": (
            "JSON：含「字段说明」与 faqs 数组；"
            "question/answer（或标准问/答案）必填"
        ),
        "media_type": "application/json; charset=utf-8",
    },
    "csv": {
        "filename": "faq_template.csv",
        "description": (
            "CSV 中文表头：编号/分类/标准问/答案/相似问；"
            "相似问用 | 分隔"
        ),
        "media_type": "text/csv; charset=utf-8",
    },
    "txt": {
        "filename": "faq_template.txt",
        "description": "文本模板：Q:/A: 必填，S: 相似问，C: 分类",
        "media_type": "text/plain; charset=utf-8",
    },
    "xls": {
        "filename": "faq_template.xls",
        "description": "Excel 97-2003：中文表头 +「字段说明」工作表",
        "media_type": "application/vnd.ms-excel",
    },
    "xlsx": {
        "filename": "faq_template.xlsx",
        "description": "Excel：中文表头 +「字段说明」工作表",
        "media_type": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    },
}

_SUFFIX_MAP = {
    ".json": "json",
    ".csv": "csv",
    ".txt": "txt",
    ".text": "txt",
    ".xls": "xls",
    ".xlsx": "xlsx",
}

_QA_PREFIX = re.compile(
    r"^(?:(Q|A|S|C|问|答|相似|分类)\s*[:：]\s*)(.*)$",
    re.IGNORECASE,
)


def list_template_meta() -> list[dict[str, str]]:
    return [
        {
            "format": fmt,
            "filename": _FORMAT_META[fmt]["filename"],
            "description": _FORMAT_META[fmt]["description"],
        }
        for fmt in SUPPORTED_FORMATS
    ]


def detect_format(name: str) -> str | None:
    suffix = Path(name or "").suffix.lower()
    return _SUFFIX_MAP.get(suffix)


def remap_entry_keys(item: dict[str, Any]) -> dict[str, Any]:
    """将中英文表头 / 字段名统一映射为内部英文键。"""
    return {_norm_header(str(k)): v for k, v in item.items() if k is not None}


def normalize_entry(item: dict[str, Any]) -> dict[str, Any] | None:
    item = remap_entry_keys(item)
    question = str(item.get("question") or "").strip()
    answer = str(item.get("answer") or "").strip()
    if not question or not answer:
        return None
    similar_raw = item.get("similar") or []
    if isinstance(similar_raw, str):
        similar = [
            p.strip()
            for p in re.split(r"[|；;]+", similar_raw)
            if p.strip()
        ]
    elif isinstance(similar_raw, list):
        similar = [str(s).strip() for s in similar_raw if str(s).strip()]
    else:
        similar = []
    out: dict[str, Any] = {
        "question": question,
        "answer": answer,
        "similar": similar,
        "category": str(item.get("category") or "").strip(),
    }
    faq_id = str(item.get("id") or "").strip()
    if faq_id:
        out["id"] = faq_id
    return out


def normalize_entries(raw_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        norm = normalize_entry(item)
        if norm:
            out.append(norm)
    return out


def _field_docs_json() -> list[dict[str, str]]:
    """JSON 模板内嵌的字段说明。"""
    return [
        {
            "字段名": d["field"],
            "中文表头": d["zh"],
            "是否必填": d["required"],
            "含义": d["meaning"],
        }
        for d in FIELD_DOCS
    ]


def parse_faq_bytes(data: bytes, fmt: str) -> list[dict[str, Any]]:
    fmt = (fmt or "").strip().lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"不支持的格式：{fmt}，可选：{', '.join(SUPPORTED_FORMATS)}")
    if fmt == "json":
        return _parse_json(data)
    if fmt == "csv":
        return _parse_csv(data)
    if fmt == "txt":
        return _parse_txt(data)
    if fmt == "xls":
        return _parse_xls(data)
    if fmt == "xlsx":
        return _parse_xlsx(data)
    raise ValueError(f"不支持的格式：{fmt}")


def parse_faq_path(path: Path) -> list[dict[str, Any]]:
    path = path.resolve()
    if not path.exists():
        raise FileNotFoundError(f"FAQ 文件不存在: {path}")
    fmt = detect_format(path.name)
    if not fmt:
        raise ValueError(
            f"无法识别文件格式：{path.name}。支持扩展名："
            ".json / .csv / .txt / .text / .xls / .xlsx"
        )
    return parse_faq_bytes(path.read_bytes(), fmt)


def parse_faq_url(url: str) -> list[dict[str, Any]]:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("url 仅支持 http:// 或 https://")
    if not parsed.netloc:
        raise ValueError("url 无效")

    fmt = detect_format(parsed.path) or "json"
    accept = {
        "json": "application/json,text/plain,*/*",
        "csv": "text/csv,text/plain,*/*",
        "txt": "text/plain,*/*",
        "xls": "application/vnd.ms-excel,*/*",
        "xlsx": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,*/*"
        ),
    }.get(fmt, "*/*")

    req = Request(
        url,
        headers={"User-Agent": "replykit/0.1", "Accept": accept},
    )
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read()
    except HTTPError as exc:
        raise ValueError(f"拉取 FAQ URL 失败: HTTP {exc.code}") from exc
    except URLError as exc:
        raise ValueError(f"拉取 FAQ URL 失败: {exc.reason}") from exc

    return parse_faq_bytes(body, fmt)


def build_template_file(fmt: str) -> tuple[bytes, str, str]:
    """Return (content, filename, media_type)."""
    fmt = (fmt or "").strip().lower()
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"未知模板格式：{fmt}")
    meta = _FORMAT_META[fmt]
    if fmt == "json":
        content = _render_json(SAMPLE_ENTRIES)
    elif fmt == "csv":
        content = _render_csv(SAMPLE_ENTRIES)
    elif fmt == "txt":
        content = _render_txt(SAMPLE_ENTRIES)
    elif fmt == "xls":
        content = _render_xls(SAMPLE_ENTRIES)
    else:
        content = _render_xlsx(SAMPLE_ENTRIES)
    return content, meta["filename"], meta["media_type"]


def _parse_json(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig")
    raw = json.loads(text)
    if isinstance(raw, dict):
        # 兼容 {"faqs": [...]} / {"数据": [...]}，忽略「字段说明」等元信息键
        for key in ("faqs", "数据", "items", "list"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            raise ValueError(
                'FAQ JSON 须为数组，或包含 faqs 数组的对象，例如 '
                '{"字段说明": {...}, "faqs": [...]}'
            )
    if not isinstance(raw, list):
        raise ValueError("FAQ JSON 须为数组，或 {\"faqs\": [...]}")
    return normalize_entries([x for x in raw if isinstance(x, dict)])


def _parse_csv(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    rows: list[dict[str, Any]] = []
    for row in reader:
        if not row:
            continue
        mapped = {_norm_header(k): (v or "").strip() for k, v in row.items() if k}
        rows.append(mapped)
    return normalize_entries(rows)


def _norm_header(name: str) -> str:
    """中英文表头 / 字段名 → 内部英文字段名。"""
    key = (name or "").strip()
    key_l = key.lower()
    aliases = {
        # id
        "id": "id",
        "faq_id": "id",
        "faqid": "id",
        "编号": "id",
        "标识": "id",
        "唯一编号": "id",
        # category
        "category": "category",
        "分类": "category",
        "类别": "category",
        # question
        "question": "question",
        "标准问": "question",
        "问题": "question",
        "问法": "question",
        "主问题": "question",
        "标题": "question",
        # answer
        "answer": "answer",
        "答案": "answer",
        "回答": "answer",
        "回复": "answer",
        "标准答案": "answer",
        # similar
        "similar": "similar",
        "similars": "similar",
        "相似问": "similar",
        "相似问题": "similar",
        "相似问法": "similar",
        "同义问": "similar",
    }
    if key in aliases:
        return aliases[key]
    if key_l in aliases:
        return aliases[key_l]
    return key_l


def _parse_txt(data: bytes) -> list[dict[str, Any]]:
    text = data.decode("utf-8-sig")
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    answer_lines: list[str] = []
    mode: str | None = None

    def flush() -> None:
        nonlocal current, answer_lines, mode
        if current is None:
            return
        if answer_lines:
            current["answer"] = "\n".join(answer_lines).strip()
        norm = normalize_entry(current)
        if norm:
            entries.append(norm)
        current = None
        answer_lines = []
        mode = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.strip() in {"---", "***", "===", ""}:
            if line.strip() in {"---", "***", "==="}:
                flush()
            elif mode == "A" and answer_lines:
                answer_lines.append("")
            continue

        m = _QA_PREFIX.match(line.strip())
        if m:
            tag, rest = m.group(1), m.group(2)
            tag_l = tag.lower()
            if tag_l in {"q", "问"}:
                flush()
                current = {
                    "question": rest.strip(),
                    "answer": "",
                    "similar": [],
                    "category": "",
                }
                answer_lines = []
                mode = "Q"
            elif tag_l in {"a", "答"}:
                if current is None:
                    current = {
                        "question": "",
                        "answer": "",
                        "similar": [],
                        "category": "",
                    }
                answer_lines = [rest] if rest.strip() else []
                mode = "A"
            elif tag_l in {"s", "相似"}:
                if current is None:
                    continue
                if rest.strip():
                    current.setdefault("similar", []).append(rest.strip())
                mode = "S"
            elif tag_l in {"c", "分类"}:
                if current is None:
                    continue
                current["category"] = rest.strip()
                mode = "C"
            continue

        if mode == "A" and current is not None:
            answer_lines.append(line)
        elif mode == "Q" and current is not None and line.strip():
            # 续行并入标准问
            current["question"] = f"{current.get('question', '')} {line.strip()}".strip()

    flush()
    return entries


def _parse_table_rows(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    keys = [_norm_header(str(h)) for h in headers]
    items: list[dict[str, Any]] = []
    for row in rows:
        if not row or all(str(c or "").strip() == "" for c in row):
            continue
        item: dict[str, Any] = {}
        for i, key in enumerate(keys):
            if not key or key.startswith("unnamed"):
                continue
            val = row[i] if i < len(row) else ""
            item[key] = "" if val is None else str(val).strip()
        items.append(item)
    return normalize_entries(items)


def _headers_look_like_faq(headers: list[str]) -> bool:
    mapped = {_norm_header(h) for h in headers if str(h or "").strip()}
    return "question" in mapped and "answer" in mapped


def _pick_excel_sheets(sheet_titles: list[str]) -> list[str]:
    """优先 FAQ数据，跳过字段说明，其余按原顺序。"""
    skip = {"字段说明", "说明"}
    preferred = [n for n in sheet_titles if n == "FAQ数据"]
    others = [n for n in sheet_titles if n not in preferred and n not in skip]
    return preferred + others + [n for n in sheet_titles if n in skip and n not in preferred]


def _parse_xlsx(data: bytes) -> list[dict[str, Any]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("解析 xlsx 需要 openpyxl，请安装依赖") from exc
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        by_title = {ws.title: ws for ws in wb.worksheets}
        if not by_title:
            raise ValueError("xlsx 为空")
        for name in _pick_excel_sheets(list(by_title.keys())):
            ws = by_title[name]
            rows_iter = ws.iter_rows(values_only=True)
            try:
                header_row = next(rows_iter)
            except StopIteration:
                continue
            headers = ["" if c is None else str(c) for c in header_row]
            if not _headers_look_like_faq(headers):
                continue
            body = [list(r) for r in rows_iter]
            return _parse_table_rows(headers, body)
        raise ValueError(
            "xlsx 中未找到有效 FAQ 表（表头需含「标准问」与「答案」，"
            "或 question 与 answer）"
        )
    finally:
        wb.close()


def _parse_xls(data: bytes) -> list[dict[str, Any]]:
    try:
        import xlrd
    except ImportError as exc:
        raise ValueError("解析 xls 需要 xlrd，请安装依赖") from exc
    book = xlrd.open_workbook(file_contents=data)
    names = book.sheet_names()
    if not names:
        raise ValueError("xls 为空")
    for name in _pick_excel_sheets(names):
        sheet = book.sheet_by_name(name)
        if sheet.nrows < 1:
            continue
        headers = [str(sheet.cell_value(0, c)) for c in range(sheet.ncols)]
        if not _headers_look_like_faq(headers):
            continue
        body = [
            [sheet.cell_value(r, c) for c in range(sheet.ncols)]
            for r in range(1, sheet.nrows)
        ]
        return _parse_table_rows(headers, body)
    raise ValueError(
        "xls 中未找到有效 FAQ 表（表头需含「标准问」与「答案」，"
        "或 question 与 answer）"
    )


def _render_json(entries: list[dict[str, Any]]) -> bytes:
    payload = {
        "字段说明": _field_docs_json(),
        "faqs": entries,
    }
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _render_csv(entries: list[dict[str, Any]]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=TABLE_HEADERS_ZH,
        lineterminator="\n",
    )
    writer.writeheader()
    for item in entries:
        writer.writerow(_entry_to_zh_row(item))
    # BOM 方便 Excel 打开中文
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _entry_to_zh_row(item: dict[str, Any]) -> dict[str, str]:
    return {
        "编号": str(item.get("id") or ""),
        "分类": str(item.get("category") or ""),
        "标准问": str(item.get("question") or ""),
        "答案": str(item.get("answer") or ""),
        "相似问": "|".join(item.get("similar") or []),
    }


def _append_field_docs_sheet_xlsx(wb: Any) -> None:
    ws = wb.create_sheet("字段说明", 0)
    ws.append(["表头（中文）", "内部字段名", "是否必填", "含义"])
    for d in FIELD_DOCS:
        ws.append([d["zh"], d["field"], d["required"], d["meaning"]])
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 14
    ws.column_dimensions["C"].width = 10
    ws.column_dimensions["D"].width = 72


def _append_field_docs_sheet_xls(book: Any) -> None:
    sheet = book.add_sheet("字段说明")
    headers = ["表头（中文）", "内部字段名", "是否必填", "含义"]
    for c, h in enumerate(headers):
        sheet.write(0, c, h)
    for r, d in enumerate(FIELD_DOCS, start=1):
        sheet.write(r, 0, d["zh"])
        sheet.write(r, 1, d["field"])
        sheet.write(r, 2, d["required"])
        sheet.write(r, 3, d["meaning"])


def _render_txt(entries: list[dict[str, Any]]) -> bytes:
    parts: list[str] = [
        "# FAQ 文本模板：每条以 Q:/A: 开头；S: 相似问（可多行）；C: 分类；条目之间用 --- 分隔",
        "# 字段说明：Q=标准问(必填) A=答案(必填) S=相似问(可选,可多行) C=分类(可选)",
        "",
    ]
    for i, item in enumerate(entries):
        if i:
            parts.append("---")
            parts.append("")
        parts.append(f"Q: {item.get('question', '')}")
        parts.append(f"A: {item.get('answer', '')}")
        for s in item.get("similar") or []:
            parts.append(f"S: {s}")
        if item.get("category"):
            parts.append(f"C: {item['category']}")
        parts.append("")
    return ("\n".join(parts)).encode("utf-8")


def _render_xlsx(entries: list[dict[str, Any]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    # 先写数据表，再把「字段说明」插到最前，打开文件先看到说明
    ws = wb.active
    ws.title = "FAQ数据"
    ws.append(list(TABLE_HEADERS_ZH))
    for item in entries:
        row = _entry_to_zh_row(item)
        ws.append([row[h] for h in TABLE_HEADERS_ZH])
    for col, width in enumerate([12, 12, 28, 48, 36], start=1):
        ws.column_dimensions[chr(64 + col)].width = width
    _append_field_docs_sheet_xlsx(wb)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def _render_xls(entries: list[dict[str, Any]]) -> bytes:
    import xlwt

    book = xlwt.Workbook(encoding="utf-8")
    _append_field_docs_sheet_xls(book)
    sheet = book.add_sheet("FAQ数据")
    for c, h in enumerate(TABLE_HEADERS_ZH):
        sheet.write(0, c, h)
    for r, item in enumerate(entries, start=1):
        row = _entry_to_zh_row(item)
        for c, h in enumerate(TABLE_HEADERS_ZH):
            sheet.write(r, c, row[h])
    bio = io.BytesIO()
    book.save(bio)
    return bio.getvalue()
