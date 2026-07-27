"""Conversation turn logs (P3-1): question / answer / score / sources / handoff.

Persists each Q&A to SQLite ``chat_logs`` for ops export (recent N rows).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

import xlwt

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


class ChatLogStore:
    """Append-only SQLite store for one Q&A turn per row."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    score REAL,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    is_handoff INTEGER NOT NULL DEFAULT 0,
                    route TEXT NOT NULL DEFAULT '',
                    strategy TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cols = {
                str(r[1])
                for r in self._conn.execute("PRAGMA table_info(chat_logs)").fetchall()
            }
            if "username" not in cols:
                self._conn.execute(
                    "ALTER TABLE chat_logs ADD COLUMN username TEXT NOT NULL DEFAULT ''"
                )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_logs_created "
                "ON chat_logs(id DESC)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_logs_created_at "
                "ON chat_logs(created_at)"
            )
            self._migrate_created_at_to_unix()

    def _migrate_created_at_to_unix(self) -> None:
        """Convert legacy ISO created_at to Unix seconds; rebuild column as INTEGER."""
        rows = self._conn.execute(
            "SELECT id, created_at FROM chat_logs"
        ).fetchall()
        for row in rows:
            raw = row["created_at"]
            if isinstance(raw, int):
                continue
            ts = _coerce_unix_ts(raw)
            if ts is None:
                continue
            self._conn.execute(
                "UPDATE chat_logs SET created_at = ? WHERE id = ?",
                (ts, row["id"]),
            )

        col_type = ""
        for col in self._conn.execute("PRAGMA table_info(chat_logs)").fetchall():
            # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
            if str(col[1]) == "created_at":
                col_type = str(col[2] or "").upper()
                break
        if col_type == "INTEGER":
            return

        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                """
                CREATE TABLE chat_logs_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL,
                    session_id TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    score REAL,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    is_handoff INTEGER NOT NULL DEFAULT 0,
                    route TEXT NOT NULL DEFAULT '',
                    strategy TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._conn.execute(
                """
                INSERT INTO chat_logs_new (
                    id, created_at, session_id, question, answer,
                    score, sources_json, is_handoff, route, strategy
                )
                SELECT
                    id,
                    CAST(created_at AS INTEGER),
                    session_id, question, answer,
                    score, sources_json, is_handoff, route, strategy
                FROM chat_logs
                """
            )
            self._conn.execute("DROP TABLE chat_logs")
            self._conn.execute("ALTER TABLE chat_logs_new RENAME TO chat_logs")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_chat_logs_created "
                "ON chat_logs(id DESC)"
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def insert(
        self,
        *,
        session_id: str,
        question: str,
        result: ChatResult,
        username: str = "",
    ) -> int:
        """Insert one turn; returns new row id. Never raises to callers — use try."""
        now = int(time.time())
        sources = list(result.sources or [])
        sources_json = json.dumps(sources, ensure_ascii=False)
        is_handoff = 1 if result.route == "handoff" else 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO chat_logs (
                        created_at, session_id, username, question, answer,
                        score, sources_json, is_handoff, route, strategy
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        now,
                        (session_id or "").strip(),
                        (username or "").strip(),
                        question or "",
                        result.answer or "",
                        result.top_score,
                        sources_json,
                        is_handoff,
                        result.route or "",
                        result.strategy or "",
                    ),
                )
                self._conn.execute("COMMIT")
                return int(cur.lastrowid or 0)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def list_page(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> ChatLogPage:
        """Paginate logs newest-first. page is 1-based."""
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        total = self.count()
        offset = (page - 1) * page_size
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, created_at, session_id, username, question, answer,
                       score, sources_json, is_handoff, route, strategy
                FROM chat_logs
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        return ChatLogPage(
            items=[_row_to_log(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def recent(self, limit: int = 100) -> list[ChatLogRow]:
        """Newest ``limit`` rows (page 1 with page_size=limit)."""
        n = max(1, min(int(limit), 10_000))
        # list_page caps page_size at 200; for export allow larger via direct query
        if n <= 200:
            return self.list_page(page=1, page_size=n).items
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, created_at, session_id, username, question, answer,
                       score, sources_json, is_handoff, route, strategy
                FROM chat_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
        return [_row_to_log(r) for r in rows]

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM chat_logs").fetchone()
        return int(row["n"] if row else 0)

    @staticmethod
    def _time_filter(
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> tuple[str, list[object]]:
        """Return SQL fragment (may be empty) and bind params for created_at window."""
        clauses: list[str] = []
        params: list[object] = []
        if start is not None:
            clauses.append("created_at >= ?")
            params.append(int(start))
        if end is not None:
            clauses.append("created_at <= ?")
            params.append(int(end))
        if not clauses:
            return "", params
        return " AND ".join(clauses), params

    def count_turns(
        self,
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> int:
        """Count turns in optional [start, end] Unix-second window."""
        time_sql, time_params = self._time_filter(start=start, end=end)
        sql = "SELECT COUNT(*) AS n FROM chat_logs"
        if time_sql:
            sql += f" WHERE {time_sql}"
        with self._lock:
            row = self._conn.execute(sql, time_params).fetchone()
        return int(row["n"] if row else 0)

    def count_matching(
        self,
        where_sql: str,
        params: list[object] | tuple[object, ...] = (),
        *,
        start: int | None = None,
        end: int | None = None,
    ) -> int:
        """Count turns matching extra WHERE (without leading AND/WHERE)."""
        extra = (where_sql or "").strip()
        time_sql, time_params = self._time_filter(start=start, end=end)
        clauses: list[str] = []
        bind: list[object] = []
        if extra:
            clauses.append(f"({extra})")
            bind.extend(params)
        if time_sql:
            clauses.append(time_sql)
            bind.extend(time_params)
        sql = "SELECT COUNT(*) AS n FROM chat_logs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._lock:
            row = self._conn.execute(sql, bind).fetchone()
        return int(row["n"] if row else 0)

    def top_questions_matching(
        self,
        where_sql: str,
        params: list[object] | tuple[object, ...] = (),
        *,
        limit: int = 10,
        start: int | None = None,
        end: int | None = None,
    ) -> list[tuple[str, int]]:
        """Group by trimmed question under a filter; return [(question, count), ...]."""
        n = max(1, min(int(limit), 100))
        extra = (where_sql or "").strip()
        time_sql, time_params = self._time_filter(start=start, end=end)
        clauses: list[str] = ["TRIM(question) != ''"]
        bind: list[object] = []
        if extra:
            clauses.append(f"({extra})")
            bind.extend(params)
        if time_sql:
            clauses.append(time_sql)
            bind.extend(time_params)
        bind.append(n)
        sql = f"""
            SELECT TRIM(question) AS q, COUNT(*) AS n
            FROM chat_logs
            WHERE {" AND ".join(clauses)}
            GROUP BY TRIM(question)
            ORDER BY n DESC, q ASC
            LIMIT ?
        """
        with self._lock:
            rows = self._conn.execute(sql, bind).fetchall()
        return [(str(r["q"]), int(r["n"])) for r in rows]

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
        """Build an .xls workbook in memory (for HTTP download)."""
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
        with self._lock:
            self._conn.close()


def _coerce_unix_ts(raw: object) -> int | None:
    """Normalize created_at to Unix seconds; None if empty/unparseable."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        return int(s)
    try:
        # legacy ISO-8601, e.g. 2026-07-22T09:52:54.357024+00:00
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return None


def _row_to_log(row: sqlite3.Row) -> ChatLogRow:
    try:
        sources_raw = json.loads(row["sources_json"] or "[]")
    except json.JSONDecodeError:
        sources_raw = []
    sources = [str(s) for s in sources_raw] if isinstance(sources_raw, list) else []
    score = row["score"]
    created = _coerce_unix_ts(row["created_at"])
    if created is None:
        created = 0
    return ChatLogRow(
        id=int(row["id"]),
        created_at=created,
        session_id=str(row["session_id"] or ""),
        username=str(row["username"] if "username" in row.keys() else "") or "",
        question=str(row["question"] or ""),
        answer=str(row["answer"] or ""),
        score=float(score) if score is not None else None,
        sources=sources,
        is_handoff=bool(row["is_handoff"]),
        route=str(row["route"] or ""),
        strategy=str(row["strategy"] or ""),
    )


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
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="日志库路径（默认读 CHAT_LOG_DB_PATH / Settings）",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    db_path = args.db or settings.chat_log_db_path
    if not Path(db_path).is_absolute():
        db_path = PROJECT_ROOT / db_path
    out = args.out or (PROJECT_ROOT / "data" / "chat_logs_export.xls")

    store = ChatLogStore(db_path)
    try:
        if args.page is not None:
            page_data = store.list_page(page=args.page, page_size=args.page_size)
            path = store.export_xls(
                out, page=args.page, page_size=args.page_size
            )
            print(f"库路径: {Path(db_path).resolve()}")
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
            print(f"库路径: {Path(db_path).resolve()}")
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
