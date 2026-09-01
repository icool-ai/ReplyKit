"""JWT auth with user/ops roles, registration, and user management (SQLAlchemy).

Now with OPTIONAL Redis-backed features:
1. JWT access-token blacklist (single-jti + user-wide revoke-before markers)
   so logout / password-change / disable-user can *instantly* invalidate
   still-valid access tokens.
2. Login rate limiting (fixed-window counter per IP + username pair) to
   prevent brute-force attacks.

Both features gracefully degrade to no-ops when the `redis` package is
not installed or ``REDIS_URL`` is not configured — core auth never breaks.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

import bcrypt
import jwt
from fastapi import HTTPException
from sqlalchemy import Engine, and_, func, or_, select
from sqlalchemy.orm import Session

from mp_agent.dao._helpers import dt_to_unix, unix_to_dt, utc_now
from mp_agent.dao._engine_normalize import normalize_store_engine
from mp_agent.dao.models import RefreshToken, User
from mp_agent.dao.redis_client import (
    RateLimitExceeded,
    get_user_revoke_before,
    is_access_blacklisted,
    login_rate_limit_increment,
    login_rate_limit_reset,
    revoke_all_access_for_user,
)
from mp_agent.dao.sync_db import sync_engine

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
    access_jti: str = ""


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
    """Users + JWT access + opaque refresh tokens (SQLAlchemy).

    Optional Redis features (zero-config fallback):
    * Access-token revocation via per-jti blacklist / user-wide marker.
    * Login brute-force rate limiting.
    """

    def __init__(
        self,
        *,
        engine: Engine | None = None,
        jwt_secret: str,
        access_ttl: int,
        refresh_ttl: int,
        admin_username: str,
        admin_password: str,
        allow_register: bool = True,
        login_rate_limit: int = 5,
        login_rate_window_sec: int = 300,
    ) -> None:
        if not jwt_secret or len(jwt_secret) < 32:
            raise ValueError("JWT_SECRET 未配置或过短（至少 32 字符）")
        if not admin_username or not admin_password:
            raise ValueError("ADMIN_USERNAME / ADMIN_PASSWORD 必须配置")
        if not _USERNAME_RE.match(admin_username):
            raise ValueError("ADMIN_USERNAME 须为 3–32 位字母、数字或下划线")

        self._engine = normalize_store_engine(engine)
        self._jwt_secret = jwt_secret
        self._access_ttl = max(60, access_ttl)
        self._refresh_ttl = max(60, refresh_ttl)
        self._allow_register = allow_register
        self._admin_username = admin_username
        self._login_rate_limit = max(1, int(login_rate_limit))
        self._login_rate_window_sec = max(10, int(login_rate_window_sec))
        self._ensure_admin(admin_username, admin_password)

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------
    def _ensure_admin(self, username: str, password: str) -> None:
        password_hash = self._hash_password(password)
        with Session(self._engine) as session:
            user = session.get(User, username)
            if user is None:
                session.add(
                    User(
                        username=username,
                        password_hash=password_hash,
                        role="ops",
                        enabled=True,
                        created_at=utc_now(),
                    )
                )
            else:
                user.role = "ops"
                user.enabled = True
            session.commit()

    # ------------------------------------------------------------------
    # Login rate limiting (场景 5)
    # ------------------------------------------------------------------
    def check_login_rate_limit(self, *, client_ip: str, username: str) -> None:
        """Raise HTTP 429 if IP+user has exceeded the login attempt window.

        Always safe: no Redis == no-op (never raises).
        """
        try:
            login_rate_limit_increment(
                ip=client_ip or "unknown",
                username=(username or "").strip() or "*",
                limit=self._login_rate_limit,
                window_seconds=self._login_rate_window_sec,
            )
        except RateLimitExceeded as exc:
            headers = {"Retry-After": str(exc.retry_after_seconds)}
            raise HTTPException(
                status_code=429,
                detail={
                    "message": f"尝试过于频繁，请 {exc.retry_after_seconds} 秒后重试",
                    "retry_after": exc.retry_after_seconds,
                    "attempts": exc.attempts,
                    "limit": exc.limit,
                    "window_seconds": exc.window_seconds,
                },
                headers=headers,
            ) from exc

    def reset_login_rate_limit(self, *, client_ip: str, username: str) -> None:
        """Clear the rate-limit counter after a successful login."""
        login_rate_limit_reset(
            ip=client_ip or "unknown",
            username=(username or "").strip() or "*",
        )

    # ------------------------------------------------------------------
    # Registration / login / token issuance
    # ------------------------------------------------------------------
    def register(self, username: str, password: str) -> TokenPair:
        if not self._allow_register:
            raise HTTPException(status_code=403, detail="当前不允许注册")
        user = self._validate_username(username)
        self._validate_password(password)
        password_hash = self._hash_password(password)

        with Session(self._engine) as session:
            if session.get(User, user) is not None:
                raise HTTPException(status_code=409, detail="用户名已存在")
            session.add(
                User(
                    username=user,
                    password_hash=password_hash,
                    role="user",
                    enabled=True,
                    created_at=utc_now(),
                )
            )
            session.commit()
        return self._issue_pair(user, "user")

    def login(
        self,
        username: str,
        password: str,
        *,
        client_ip: str = "",
    ) -> TokenPair:
        """Verify credentials and issue tokens.

        Also enforces login-rate limiting:
        * Every call (success or failure) bumps the attempt counter.
        * On success the counter is cleared so the next session starts clean.
        """
        user = (username or "").strip()
        self.check_login_rate_limit(client_ip=client_ip, username=user)
        try:
            with Session(self._engine) as session:
                db_user = session.get(User, user)
                if db_user is None or not db_user.enabled:
                    raise HTTPException(status_code=401, detail="用户名或密码错误")
                if not self._verify_password(password, db_user.password_hash):
                    raise HTTPException(status_code=401, detail="用户名或密码错误")
                role = self._normalize_role(db_user.role)
        except HTTPException:
            raise
        pair = self._issue_pair(user, role)
        self.reset_login_rate_limit(client_ip=client_ip, username=user)
        return pair

    def refresh(self, refresh_token: str) -> TokenPair:
        token = (refresh_token or "").strip()
        if not token:
            raise HTTPException(status_code=401, detail="refresh_token 无效或已过期")

        with Session(self._engine) as session:
            rt = session.scalar(
                select(RefreshToken).where(RefreshToken.token == token)
            )
            now = utc_now()
            if rt is None or rt.expires_at < now:
                if rt is not None:
                    session.delete(rt)
                    session.commit()
                raise HTTPException(
                    status_code=401, detail="refresh_token 无效或已过期"
                )

            db_user = session.get(User, rt.username)
            if db_user is None or not db_user.enabled:
                session.delete(rt)
                session.commit()
                raise HTTPException(
                    status_code=401, detail="refresh_token 无效或已过期"
                )
            role = self._normalize_role(db_user.role)
            session.delete(rt)
            session.commit()
        return self._issue_pair(rt.username, role)

    # ------------------------------------------------------------------
    # Logout + access token revocation (场景 1)
    # ------------------------------------------------------------------
    def logout(
        self,
        refresh_token: str,
        *,
        access_jti: str | None = None,
        access_remaining_ttl: int | None = None,
        username: str | None = None,
    ) -> bool:
        """Invalidate a refresh token (DB) and optionally the access token (Redis).

        Three revocation modes, strongest-first:
        1. ``username`` provided    → revoke *all* access tokens for the user
           (via "revoke_all before now" marker).  Most idiomatic: a user
           clicking "logout" usually means "kick all my sessions".
        2. ``access_jti`` + ttl     → blacklist a single JTI.
        3. Neither (Redis down)     → only delete the refresh token from DB;
           existing access tokens stay valid until their natural expiry
           (original behaviour, safe fallback).
        """
        token = (refresh_token or "").strip()
        if token:
            with Session(self._engine) as session:
                rt = session.scalar(
                    select(RefreshToken).where(RefreshToken.token == token)
                )
                if rt is not None:
                    resolved_user = username or rt.username
                    session.delete(rt)
                    session.commit()
                    # 登出 = 用户明确退出，不需要给新 token 留 1s 余量
                    # (用户要重新登录才能拿到新 token)，所以 margin=0 最严格
                    revoke_all_access_for_user(
                        resolved_user,
                        ttl_seconds=self._access_ttl,
                        now_margin_seconds=0,
                    )
                    return True
        # Fallback: refresh token was empty / not found, still try user-wide revoke
        if username:
            revoke_all_access_for_user(
                username,
                ttl_seconds=self._access_ttl,
                now_margin_seconds=0,
            )
        if access_jti and access_remaining_ttl:
            from mp_agent.dao.redis_client import blacklist_access_jti
            blacklist_access_jti(access_jti, ttl_seconds=access_remaining_ttl)
        return True

    # ------------------------------------------------------------------
    # Access-token decoding + blacklist check (场景 1)
    # ------------------------------------------------------------------
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
        # ---------- Redis 黑名单检查 ----------
        # (a) 单 JTI 精确拉黑
        jti = str(payload.get("jti") or "").strip()
        if jti and is_access_blacklisted(jti):
            raise HTTPException(
                status_code=401,
                detail="access_token 已被吊销",
            )
        # (b) 用户级 "iat <= revoke_before" 粗粒度拉黑
        # marker = now-1（见 redis_client.revoke_all_access_for_user），语义：
        #   「marker 时间戳及之前签发的 token 全部作废」
        # -1s 安全余量保证：改密/禁用后立刻用新密码登录拿到的新 token（iat = now）
        # 不会 <= now-1，不会被自己触发的 marker 误杀。
        # 同时 TTL = access_ttl 保证所有真实在飞老 token 一定在 marker 窗口内。
        revoke_before = get_user_revoke_before(sub)
        if revoke_before:
            iat = int(payload.get("iat") or 0)
            if iat and iat <= revoke_before:
                raise HTTPException(
                    status_code=401,
                    detail="access_token 已被吊销（用户级吊销）",
                )
        role = self._normalize_role(payload.get("role"))
        return {"sub": sub, "role": role, **payload}

    # ------------------------------------------------------------------
    # User CRUD (ops)
    # ------------------------------------------------------------------
    def create_user(
        self,
        *,
        actor: str,
        username: str,
        password: str,
        role: Role = "user",
    ) -> UserRow:
        _ = actor
        user = self._validate_username(username)
        self._validate_password(password)
        role = self._normalize_role(role)
        password_hash = self._hash_password(password)

        with Session(self._engine) as session:
            if session.get(User, user) is not None:
                raise HTTPException(status_code=409, detail="用户名已存在")
            session.add(
                User(
                    username=user,
                    password_hash=password_hash,
                    role=role,
                    enabled=True,
                    created_at=utc_now(),
                )
            )
            session.commit()
            created = session.get(User, user)
            assert created is not None
            return _user_to_row(created)

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

        with Session(self._engine) as session:
            filters: list[Any] = []
            kw = (keyword or "").strip()
            if kw:
                filters.append(User.username.ilike(f"%{kw}%"))
            if role:
                filters.append(User.role == self._normalize_role(role))
            if enabled is not None:
                filters.append(User.enabled.is_(bool(enabled)))

            count_stmt = select(func.count()).select_from(User)
            stmt = select(User)
            if filters:
                for f in filters:
                    count_stmt = count_stmt.where(f)
                    stmt = stmt.where(f)

            total = session.scalar(count_stmt) or 0
            offset = (page - 1) * page_size
            stmt = stmt.order_by(User.created_at.desc(), User.username.asc())
            stmt = stmt.limit(page_size).offset(offset)
            rows = session.execute(stmt).scalars().all()
            return UserPage(
                items=[_user_to_row(r) for r in rows],
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

        with Session(self._engine) as session:
            db_user = session.get(User, user)
            if db_user is None:
                raise HTTPException(status_code=404, detail="用户不存在")

            old_role = self._normalize_role(db_user.role)
            new_role = self._normalize_role(role if role is not None else db_user.role)
            new_enabled = bool(enabled) if enabled is not None else db_user.enabled
            new_hash = (
                self._hash_password(password)
                if password is not None
                else db_user.password_hash
            )

            if user == actor and (not new_enabled or new_role != "ops"):
                if not new_enabled:
                    raise HTTPException(
                        status_code=422, detail="不能禁用当前登录账号"
                    )
                if new_role != "ops" and old_role == "ops":
                    raise HTTPException(
                        status_code=422, detail="不能取消自己的运营身份"
                    )

            if old_role == "ops" and (new_role != "ops" or not new_enabled):
                ops_count = session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(and_(User.role == "ops", User.enabled.is_(True)))
                ) or 0
                if ops_count <= 1:
                    raise HTTPException(
                        status_code=422,
                        detail="不能移除最后一个运营账号",
                    )

            db_user.role = new_role
            db_user.enabled = new_enabled
            db_user.password_hash = new_hash

            need_revoke = False
            revoke_margin = 0
            if not new_enabled:
                need_revoke = True
                revoke_margin = 0  # 禁用用户 = 激进吊销（包括这一秒内所有 token）
            if password is not None:
                need_revoke = True
                revoke_margin = max(revoke_margin, 1)  # 改密后要留 1s 余量给新密码登录拿的新 token
            if need_revoke:
                rts = session.execute(
                    select(RefreshToken).where(RefreshToken.username == user)
                ).scalars().all()
                for rt in rts:
                    session.delete(rt)

            session.commit()

            # 改密 / 禁用 → 立刻拉黑该用户所有现存 access token
            if need_revoke:
                revoke_all_access_for_user(
                    user,
                    ttl_seconds=self._access_ttl,
                    now_margin_seconds=revoke_margin,
                )

            updated = session.get(User, user)
            assert updated is not None
            return _user_to_row(updated)

    def reset_password(self, *, actor: str, username: str, new_password: str) -> UserRow:
        _ = actor
        user = (username or "").strip()
        if not user:
            raise HTTPException(status_code=422, detail="username 不能为空")
        self._validate_password(new_password)
        new_hash = self._hash_password(new_password)

        with Session(self._engine) as session:
            db_user = session.get(User, user)
            if db_user is None:
                raise HTTPException(status_code=404, detail="用户不存在")
            db_user.password_hash = new_hash
            rts = session.execute(
                select(RefreshToken).where(RefreshToken.username == user)
            ).scalars().all()
            for rt in rts:
                session.delete(rt)
            session.commit()
            # 必须在 session 关闭之前把所有 ORM 属性读出来（做成 dataclass），
            # 否则 session 关了再访问 db_user.username 会抛 DetachedInstanceError。
            refreshed = session.get(User, user)
            assert refreshed is not None
            row = _user_to_row(refreshed)
        # 重置密码 → 用户级 access 全拉黑（留 1s 余量给新密码登录用的新 token）
        revoke_all_access_for_user(
            user,
            ttl_seconds=self._access_ttl,
            now_margin_seconds=1,
        )
        return row

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_role(role: Any) -> Role:
        value = str(role or "user").strip().lower()
        if value not in ROLES:
            return "user"
        return value  # type: ignore[return-value]

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
        now_unix = int(time.time())
        now_dt = unix_to_dt(now_unix)
        # 每个 access token 带唯一 JTI，支持精确拉黑
        access_jti = secrets.token_urlsafe(24)
        access = jwt.encode(
            {
                "sub": username,
                "role": role,
                "typ": "access",
                "jti": access_jti,
                "iat": now_unix,
                "exp": now_unix + self._access_ttl,
            },
            self._jwt_secret,
            algorithm="HS256",
        )
        refresh = secrets.token_urlsafe(48)
        expires_at = unix_to_dt(now_unix + self._refresh_ttl)
        with Session(self._engine) as session:
            session.add(
                RefreshToken(
                    token=refresh,
                    username=username,
                    expires_at=expires_at,
                    created_at=now_dt,
                )
            )
            session.commit()
        return TokenPair(
            access_token=access,
            token_type="Bearer",
            expires_in=self._access_ttl,
            refresh_token=refresh,
            refresh_expires_in=self._refresh_ttl,
            username=username,
            role=role,
            access_jti=access_jti,
        )


def _user_to_row(db_user: User) -> UserRow:
    return UserRow(
        username=db_user.username,
        role=AuthService._normalize_role(db_user.role),
        enabled=bool(db_user.enabled),
        created_at=dt_to_unix(db_user.created_at),
    )
