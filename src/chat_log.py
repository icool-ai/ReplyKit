"""Conversation turn logs: question / answer / score / sources / handoff (SQLAlchemy).

Persists each Q&A to ``chat_logs`` for ops export (recent N rows).
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import xlwt
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session

from mp_agent.dao._helpers import dt_to_unix, unix_to_dt, utc_now
from mp_agent.dao._engine_normalize import normalize_store_engine
from mp_agent.dao.models import ChatLog
from mp_agent.dao.sync_db import sync_engine
from src.chatbot import ChatResult
from src.config import PROJECT_ROOT, get_settings


@dataclass(frozen=True)
class ChatLogRow:
    id: int
    created_at: int  # Unix 时间戳（秒）
    session_id: str
    username: str
    question: str
    answer: str
    score: float | None
    sources: list[str]
    is_handoff: bool
    route: str
    strategy: str


@dataclass(frozen=True)
class ChatLogPage:
    items: list[ChatLogRow]
    total: int
    page: int
    page_size: int

    @property
    def total_pages(self) -> int:
        if self.page_size <= 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages

    @property
    def has_prev(self) -> bool:
        return self.page > 1


def _model_to_row(m: ChatLog) -> ChatLogRow:
    score: float | None = None
    if m.score is not None:
        try:
            score = float(m.score)
        except (TypeError, ValueError):
            score = None
    sources_raw = m.sources or []
    sources = [str(s) for s in sources_raw] if isinstance(sources_raw, list) else []
    return ChatLogRow(
        id=int(m.id),
        created_at=dt_to_unix(m.created_at),
        session_id=str(m.session_id or ""),
        username=str(m.username or ""),
        question=str(m.question or ""),
        answer=str(m.answer or ""),
        score=score,
        sources=sources,
        is_handoff=bool(m.is_handoff),
        route=str(m.route or ""),
        strategy=str(m.strategy or ""),
    )


class ChatLogStore:
    """Append-only store for one Q&A turn per row (SQLAlchemy)."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = normalize_store_engine(engine)

    def insert(
        self,
        *,
        session_id: str,
        question: str,
        result: ChatResult,
        username: str = "",
    ) -> int:
        """Insert one turn; returns new row id."""
        sources = list(result.sources or [])
        with Session(self._engine) as db:
            log = ChatLog(
                created_at=utc_now(),
                session_id=(session_id or "").strip(),
                username=(username or "").strip(),
                question=question or "",
                answer=result.answer or "",
                score=result.top_score,
                sources=sources,
                is_handoff=(result.route == "handoff"),
                route=result.route or "",
                strategy=result.strategy or "",
            )
            db.add(log)
            db.commit()
            return int(log.id)

    def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> ChatLogPage:
        """Paginate logs newest-first. page is 1-based."""
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        offset = (page - 1) * page_size
        with Session(self._engine) as db:
            total = db.scalar(select(func.count()).select_from(ChatLog)) or 0
            rows = db.execute(
                select(ChatLog).order_by(ChatLog.id.desc()).limit(page_size).offset(offset)
            ).scalars().all()
            return ChatLogPage(
                items=[_model_to_row(r) for r in rows],
                total=total,
                page=page,
                page_size=page_size,
            )

    def recent(self, limit: int = 100) -> list[ChatLogRow]:
        """Newest ``limit`` rows."""
        n = max(1, min(int(limit), 10_000))
        with Session(self._engine) as db:
            rows = db.execute(
                select(ChatLog).order_by(ChatLog.id.desc()).limit(n)
            ).scalars().all()
            return [_model_to_row(r) for r in rows]

    def count(self) -> int:
        with Session(self._engine) as db:
            return db.scalar(select(func.count()).select_from(ChatLog)) or 0

    @staticmethod
    def _time_clauses(
        start: int | None = None,
        end: int | None = None,
    ) -> list[Any]:
        clauses: list[Any] = []
        if start is not None:
            clauses.append(ChatLog.created_at >= unix_to_dt(int(start)))
        if end is not None:
            clauses.append(ChatLog.created_at <= unix_to_dt(int(end)))
        return clauses

    def count_turns(
        self,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> int:
        clauses = self._time_clauses(start=start, end=end)
        with Session(self._engine) as db:
            stmt = select(func.count()).select_from(ChatLog)
            for c in clauses:
                stmt = stmt.where(c)
            return db.scalar(stmt) or 0

    def count_matching(
        self,
        where_sql: str,
        params: list[Any] | tuple[Any, ...] = (),
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> int:
        """Count turns matching an extra raw WHERE fragment (without leading WHERE/AND)."""
        clauses = self._time_clauses(start=start, end=end)
        extra = (where_sql or "").strip()
        with Session(self._engine) as db:
            stmt = select(func.count()).select_from(ChatLog)
            for c in clauses:
                stmt = stmt.where(c)
            if extra:
                stmt = stmt.where(text(extra))
            return db.scalar(stmt.bindparams(*tuple(params))) or 0

    def top_questions_matching(
        self,
        where_sql: str,
        params: list[Any] | tuple[Any, ...] = (),
        *,
        limit: int = 10,
        start: int | None = None,
        end: int | None = None,
    ) -> list[tuple[str, int]]:
        n = max(1, min(int(limit), 100))
        clauses = self._time_clauses(start=start, end=end)
        clauses.append(func.trim(ChatLog.question) != "")
        extra = (where_sql or "").strip()
        with Session(self._engine) as db:
            q = func.trim(ChatLog.question).label("q")
            stmt = select(q, func.count().label("n")).select_from(ChatLog)
            for c in clauses:
                stmt = stmt.where(c)
            if extra:
                stmt = stmt.where(text(extra))
            stmt = stmt.group_by(q).order_by(text("n DESC"), q.asc()).limit(n)
            rows = db.execute(stmt.bindparams(*tuple(params))).all()
            return [(str(r[0]), int(r[1])) for r in rows]

    def _rows_for_export(
        self,
        *,
        limit: int = 100,
        page: int | None = None,
        page_size: int | None = None,
    ) -> list[ChatLogRow]:
        if page is not None:
            size = page_size if page_size is not None else 20
            return self.list_page(page=page, page_size=size).items
        return self.recent(limit=limit)

    @staticmethod
    def _workbook_from_rows(rows: list[ChatLogRow]) -> xlwt.Workbook:
        fieldnames = [
            "id",
            "created_at",
            "session_id",
            "username",
            "question",
            "answer",
            "score",
            "sources",
            "is_handoff",
            "route",
            "strategy",
        ]
        book = xlwt.Workbook(encoding="utf-8")
        sheet = book.add_sheet("chat_logs")
        for col, name in enumerate(fieldnames):
            sheet.write(0, col, name)
        for row_idx, r in enumerate(rows, start=1):
            values = [
                r.id,
                r.created_at,
                r.session_id,
                r.username,
                r.question,
                r.answer,
                "" if r.score is None else f"{r.score:.4f}",
                " | ".join(r.sources),
                1 if r.is_handoff else 0,
                r.route,
                r.strategy,
            ]
            for col, value in enumerate(values):
                sheet.write(row_idx, col, value)
        return book

    def export_xls_bytes(
        self,
        *,
        limit: int = 100,
        page: int | None = None,
        page_size: int | None = None,
    ) -> bytes:
        from io import BytesIO

        rows = self._rows_for_export(
            limit=limit, page=page, page_size=page_size
        )
        bio = BytesIO()
        self._workbook_from_rows(rows).save(bio)
        return bio.getvalue()

    def export_xls(
        self,
        out_path: Path,
        *,
        limit: int = 100,
        page: int | None = None,
        page_size: int | None = None,
    ) -> Path:
        path = Path(out_path)
        if path.suffix.lower() != ".xls":
            path = path.with_suffix(".xls")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = self.export_xls_bytes(
            limit=limit, page=page, page_size=page_size
        )
        path.write_bytes(data)
        return path.resolve()

    def close(self) -> None:
        return None


def log_turn_safe(
    store: ChatLogStore,
    *,
    session_id: str,
    question: str,
    result: ChatResult,
    username: str = "",
) -> None:
    """Write a turn; swallow errors so chat never fails because of logging."""
    try:
        store.insert(
            session_id=session_id,
            question=question,
            result=result,
            username=username,
        )
    except Exception as exc:  # noqa: BLE001 — ops path must not break chat
        print(f"[chat_log] 写入失败（已忽略）: {exc}", flush=True)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="导出 / 分页查看对话日志")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="导出条数（默认 100；与 --page 互斥时优先 --page）",
    )
    parser.add_argument(
        "--page",
        type=int,
        default=None,
        help="页码（从 1 开始）。指定后按分页导出/预览",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=20,
        help="每页条数（默认 20，最大 200）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="XLS 输出路径（默认 data/chat_logs_export.xls）",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    _ = settings  # engine 已全局走 sync_engine
    out = args.out or (PROJECT_ROOT / "data" / "chat_logs_export.xls")

    store = ChatLogStore()
    try:
        if args.page is not None:
            page_data = store.list_page(page=args.page, page_size=args.page_size)
            path = store.export_xls(
                out, page=args.page, page_size=args.page_size
            )
            print(
                f"分页: page={page_data.page}/{page_data.total_pages or 1} "
                f"page_size={page_data.page_size} total={page_data.total} "
                f"has_next={page_data.has_next}"
            )
            print(f"已导出本页 {len(page_data.items)} 条 → {path}")
            rows = page_data.items[:5]
        else:
            total = store.count()
            path = store.export_xls(out, limit=args.limit)
            rows = store.recent(limit=min(args.limit, 5))
            print(f"总条数: {total}")
            print(f"已导出最近 {min(args.limit, total)} 条 → {path}")
        if rows:
            print("--- 预览（最多 5 条，按 id 倒序）---")
            for r in rows:
                score = "—" if r.score is None else f"{r.score:.4f}"
                handoff = "是" if r.is_handoff else "否"
                q = r.question.replace("\n", " ")[:60]
                print(
                    f"#{r.id} handoff={handoff} score={score} "
                    f"route={r.route} q={q!r}"
                )
    finally:
        store.close()


if __name__ == "__main__":
    main()
