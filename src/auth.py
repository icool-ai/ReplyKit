"""JWT auth with user/ops roles, registration, and user management."""

from __future__ import annotations

import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal

import bcrypt
import jwt
from fastapi import HTTPException

Role = Literal["user", "ops"]
ROLES = frozenset({"user", "ops"})
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    username: str
    role: Role


@dataclass(frozen=True)
class UserRow:
    username: str
    role: Role
    enabled: bool
    created_at: int


@dataclass(frozen=True)
class UserPage:
    items: list[UserRow]
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


class AuthService:
    """SQLite users (role) + JWT access + opaque refresh tokens."""

    def __init__(
        self,
        *,
        db_path: Path,
        jwt_secret: str,
        access_ttl: int,
        refresh_ttl: int,
        admin_username: str,
        admin_password: str,
        allow_register: bool = True,
    ) -> None:
        if not jwt_secret or len(jwt_secret) < 32:
            raise ValueError("JWT_SECRET 未配置或过短（至少 32 字符）")
        if not admin_username or not admin_password:
            raise ValueError("ADMIN_USERNAME / ADMIN_PASSWORD 必须配置")
        if not _USERNAME_RE.match(admin_username):
            raise ValueError("ADMIN_USERNAME 须为 3–32 位字母、数字或下划线")

        self._db_path = db_path
        self._jwt_secret = jwt_secret
        self._access_ttl = max(60, access_ttl)
        self._refresh_ttl = max(60, refresh_ttl)
        self._allow_register = allow_register
        self._admin_username = admin_username
        self._lock = Lock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._ensure_admin(admin_username, admin_password)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        username TEXT PRIMARY KEY,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'user',
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at INTEGER NOT NULL
                    )
                    """
                )
                cols = {
                    str(r[1])
                    for r in conn.execute("PRAGMA table_info(users)").fetchall()
                }
                if "role" not in cols:
                    conn.execute(
                        "ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'"
                    )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS refresh_tokens (
                        token TEXT PRIMARY KEY,
                        username TEXT NOT NULL,
                        expires_at INTEGER NOT NULL,
                        created_at INTEGER NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _ensure_admin(self, username: str, password: str) -> None:
        now = int(time.time())
        password_hash = self._hash_password(password)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT username, role FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        """
                        INSERT INTO users
                            (username, password_hash, role, enabled, created_at)
                        VALUES (?, ?, 'ops', 1, ?)
                        """,
                        (username, password_hash, now),
                    )
                else:
                    # 种子账号保持运营身份（不改密码，避免覆盖已改密）
                    conn.execute(
                        "UPDATE users SET role = 'ops', enabled = 1 WHERE username = ?",
                        (username,),
                    )
                conn.commit()
            finally:
                conn.close()

    def register(self, username: str, password: str) -> TokenPair:
        if not self._allow_register:
            raise HTTPException(status_code=403, detail="当前不允许注册")
        user = self._validate_username(username)
        self._validate_password(password)
        now = int(time.time())
        password_hash = self._hash_password(password)
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT username FROM users WHERE username = ?",
                    (user,),
                ).fetchone()
                if existing is not None:
                    raise HTTPException(status_code=409, detail="用户名已存在")
                try:
                    conn.execute(
                        """
                        INSERT INTO users
                            (username, password_hash, role, enabled, created_at)
                        VALUES (?, ?, 'user', 1, ?)
                        """,
                        (user, password_hash, now),
                    )
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    raise HTTPException(
                        status_code=409, detail="用户名已存在"
                    ) from exc
            finally:
                conn.close()
        return self._issue_pair(user, "user")

    def login(self, username: str, password: str) -> TokenPair:
        user = (username or "").strip()
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT password_hash, enabled, role
                    FROM users WHERE username = ?
                    """,
                    (user,),
                ).fetchone()
            finally:
                conn.close()
        if row is None or not int(row["enabled"]):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        if not self._verify_password(password, str(row["password_hash"])):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        role = self._normalize_role(row["role"])
        return self._issue_pair(user, role)

    def refresh(self, refresh_token: str) -> TokenPair:
        token = (refresh_token or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="refresh_token 无效或已过期")
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT username, expires_at FROM refresh_tokens WHERE token = ?",
                    (token,),
                ).fetchone()
                if row is None or int(row["expires_at"]) < now:
                    if row is not None:
                        conn.execute(
                            "DELETE FROM refresh_tokens WHERE token = ?", (token,)
                        )
                        conn.commit()
                    raise HTTPException(
                        status_code=401, detail="refresh_token 无效或已过期"
                    )
                username = str(row["username"])
                user_row = conn.execute(
                    "SELECT enabled, role FROM users WHERE username = ?",
                    (username,),
                ).fetchone()
                if user_row is None or not int(user_row["enabled"]):
                    conn.execute(
                        "DELETE FROM refresh_tokens WHERE token = ?", (token,)
                    )
                    conn.commit()
                    raise HTTPException(
                        status_code=401, detail="refresh_token 无效或已过期"
                    )
                role = self._normalize_role(user_row["role"])
                conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
                conn.commit()
            finally:
                conn.close()
        return self._issue_pair(username, role)

    def logout(self, refresh_token: str) -> bool:
        token = (refresh_token or "").strip()
        if not token:
            return True
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM refresh_tokens WHERE token = ?", (token,))
                conn.commit()
            finally:
                conn.close()
        return True

    def decode_access(self, token: str) -> dict[str, Any]:
        try:
            payload = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=["HS256"],
            )
        except jwt.ExpiredSignatureError as exc:
            raise HTTPException(
                status_code=401, detail="access_token 已过期"
            ) from exc
        except jwt.InvalidTokenError as exc:
            raise HTTPException(
                status_code=401,
                detail="未授权：请提供有效的 Bearer Token",
            ) from exc
        if payload.get("typ") != "access":
            raise HTTPException(
                status_code=401,
                detail="未授权：请提供有效的 Bearer Token",
            )
        sub = str(payload.get("sub") or "").strip()
        if not sub:
            raise HTTPException(
                status_code=401,
                detail="未授权：请提供有效的 Bearer Token",
            )
        role = self._normalize_role(payload.get("role"))
        return {"sub": sub, "role": role, **payload}

    def create_user(
        self,
        *,
        actor: str,
        username: str,
        password: str,
        role: Role = "user",
    ) -> UserRow:
        """Ops creates a user (may set role=ops)."""
        user = self._validate_username(username)
        self._validate_password(password)
        role = self._normalize_role(role)
        now = int(time.time())
        password_hash = self._hash_password(password)
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT username FROM users WHERE username = ?",
                    (user,),
                ).fetchone()
                if existing is not None:
                    raise HTTPException(status_code=409, detail="用户名已存在")
                try:
                    conn.execute(
                        """
                        INSERT INTO users
                            (username, password_hash, role, enabled, created_at)
                        VALUES (?, ?, ?, 1, ?)
                        """,
                        (user, password_hash, role, now),
                    )
                    conn.commit()
                except sqlite3.IntegrityError as exc:
                    raise HTTPException(
                        status_code=409, detail="用户名已存在"
                    ) from exc
                row = conn.execute(
                    """
                    SELECT username, role, enabled, created_at
                    FROM users WHERE username = ?
                    """,
                    (user,),
                ).fetchone()
            finally:
                conn.close()
        assert row is not None
        _ = actor
        return self._row_to_user(row)

    def list_users(
        self,
        *,
        page: int = 1,
        page_size: int = 20,
        keyword: str | None = None,
        role: str | None = None,
        enabled: bool | None = None,
    ) -> UserPage:
        page = max(1, int(page))
        page_size = max(1, min(int(page_size), 200))
        where: list[str] = []
        params: list[Any] = []
        kw = (keyword or "").strip()
        if kw:
            where.append("username LIKE ?")
            params.append(f"%{kw}%")
        if role:
            where.append("role = ?")
            params.append(self._normalize_role(role))
        if enabled is not None:
            where.append("enabled = ?")
            params.append(1 if enabled else 0)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        with self._lock:
            conn = self._connect()
            try:
                total = int(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM users {where_sql}",
                        params,
                    ).fetchone()["n"]
                )
                offset = (page - 1) * page_size
                rows = conn.execute(
                    f"""
                    SELECT username, role, enabled, created_at
                    FROM users
                    {where_sql}
                    ORDER BY created_at DESC, username ASC
                    LIMIT ? OFFSET ?
                    """,
                    [*params, page_size, offset],
                ).fetchall()
            finally:
                conn.close()
        return UserPage(
            items=[self._row_to_user(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )

    def update_user(
        self,
        *,
        actor: str,
        username: str,
        role: Role | None = None,
        enabled: bool | None = None,
        password: str | None = None,
    ) -> UserRow:
        user = (username or "").strip()
        if not user:
            raise HTTPException(status_code=422, detail="username 不能为空")
        if role is None and enabled is None and password is None:
            raise HTTPException(status_code=422, detail="至少需要更新一个字段")
        if password is not None:
            self._validate_password(password)
        if role is not None:
            role = self._normalize_role(role)

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT username, role, enabled, created_at, password_hash
                    FROM users WHERE username = ?
                    """,
                    (user,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="用户不存在")

                new_role = self._normalize_role(role if role is not None else row["role"])
                new_enabled = (
                    bool(enabled) if enabled is not None else bool(int(row["enabled"]))
                )
                new_hash = (
                    self._hash_password(password)
                    if password is not None
                    else str(row["password_hash"])
                )

                # 不可禁用/降级自己；不可去掉最后一个 ops
                if user == actor and (not new_enabled or new_role != "ops"):
                    if not new_enabled:
                        raise HTTPException(
                            status_code=422, detail="不能禁用当前登录账号"
                        )
                    if new_role != "ops" and self._normalize_role(row["role"]) == "ops":
                        raise HTTPException(
                            status_code=422, detail="不能取消自己的运营身份"
                        )

                if (
                    self._normalize_role(row["role"]) == "ops"
                    and (new_role != "ops" or not new_enabled)
                ):
                    ops_count = int(
                        conn.execute(
                            "SELECT COUNT(*) AS n FROM users "
                            "WHERE role = 'ops' AND enabled = 1"
                        ).fetchone()["n"]
                    )
                    if ops_count <= 1:
                        raise HTTPException(
                            status_code=422,
                            detail="不能移除最后一个运营账号",
                        )

                conn.execute(
                    """
                    UPDATE users
                    SET role = ?, enabled = ?, password_hash = ?
                    WHERE username = ?
                    """,
                    (new_role, 1 if new_enabled else 0, new_hash, user),
                )
                if not new_enabled or password is not None:
                    conn.execute(
                        "DELETE FROM refresh_tokens WHERE username = ?", (user,)
                    )
                conn.commit()
                updated = conn.execute(
                    """
                    SELECT username, role, enabled, created_at
                    FROM users WHERE username = ?
                    """,
                    (user,),
                ).fetchone()
            finally:
                conn.close()
        assert updated is not None
        return self._row_to_user(updated)

    def reset_password(self, *, actor: str, username: str, new_password: str) -> UserRow:
        """Ops resets a user's password (forgot-password assistance)."""
        _ = actor  # reserved for audit; auth already enforced at API layer
        user = (username or "").strip()
        if not user:
            raise HTTPException(status_code=422, detail="username 不能为空")
        self._validate_password(new_password)
        new_hash = self._hash_password(new_password)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT username, role, enabled, created_at
                    FROM users WHERE username = ?
                    """,
                    (user,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail="用户不存在")
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE username = ?",
                    (new_hash, user),
                )
                conn.execute(
                    "DELETE FROM refresh_tokens WHERE username = ?",
                    (user,),
                )
                conn.commit()
                return self._row_to_user(row)
            finally:
                conn.close()

    @staticmethod
    def _normalize_role(role: Any) -> Role:
        value = str(role or "user").strip().lower()
        if value not in ROLES:
            return "user"
        return value  # type: ignore[return-value]

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> UserRow:
        return UserRow(
            username=str(row["username"]),
            role=AuthService._normalize_role(row["role"]),
            enabled=bool(int(row["enabled"])),
            created_at=int(row["created_at"]),
        )

    @staticmethod
    def _validate_username(username: str) -> str:
        user = (username or "").strip()
        if not _USERNAME_RE.match(user):
            raise HTTPException(
                status_code=422,
                detail="用户名须为 3–32 位字母、数字或下划线",
            )
        return user

    @staticmethod
    def _validate_password(password: str) -> None:
        if len(password or "") < 6:
            raise HTTPException(status_code=422, detail="密码至少 6 位")

    @staticmethod
    def _hash_password(password: str) -> str:
        return bcrypt.hashpw(
            password.encode("utf-8"),
            bcrypt.gensalt(),
        ).decode("utf-8")

    @staticmethod
    def _verify_password(password: str, password_hash: str) -> bool:
        try:
            return bcrypt.checkpw(
                (password or "").encode("utf-8"),
                password_hash.encode("utf-8"),
            )
        except ValueError:
            return False

    def _issue_pair(self, username: str, role: Role) -> TokenPair:
        now = int(time.time())
        access = jwt.encode(
            {
                "sub": username,
                "role": role,
                "typ": "access",
                "iat": now,
                "exp": now + self._access_ttl,
            },
            self._jwt_secret,
            algorithm="HS256",
        )
        refresh = secrets.token_urlsafe(48)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO refresh_tokens (token, username, expires_at, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (refresh, username, now + self._refresh_ttl, now),
                )
                conn.commit()
            finally:
                conn.close()
        return TokenPair(
            access_token=access,
            token_type="Bearer",
            expires_in=self._access_ttl,
            refresh_token=refresh,
            refresh_expires_in=self._refresh_ttl,
            username=username,
            role=role,
        )
