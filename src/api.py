"""FastAPI HTTP surface for the customer service bot (P2).

Share the same ``CustomerServiceBot`` logic as the Vue front-end / external clients.
Sessions persist in SQLite (``SESSION_DB_PATH``).
Images are exposed as ``/assets/...`` URLs (P2-4).
Auth: JWT Bearer after ``POST /auth/login`` (no X-API-Key).

JSON responses use a unified envelope: ``{code, message, data}``.
File download responses (e.g. FAQ templates) remain raw binary.
"""
from __future__ import annotations

import os
import threading
import logging
import uuid
from typing import Any

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Security, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from src.api_response import ApiResponse, ok, register_exception_handlers
from src.asset_urls import paths_to_asset_urls
from src.auth import AuthService
from src.bot_config import (
    get_bot_scripts,
    load_bot_scripts_template,
    reset_bot_scripts_from_template,
    save_bot_scripts,
)
from src.channels.wecom import (
    WeComCryptoError,
    decrypt_post,
    encrypt_text_reply,
    format_bot_answer,
    session_id_for_user,
    verify_url_echo,
    wecom_configured,
)
from src.chat_log import ChatLogStore, log_turn_safe
from src.chatbot import CustomerServiceBot
from src.config import Settings, get_settings
from src.faq_import import (
    SUPPORTED_FORMATS,
    build_template_file,
    detect_format,
    list_template_meta,
    parse_faq_bytes,
)
from src.faq_store import (
    FaqStore,
    load_faq_entries_from_path,
    load_faq_entries_from_url,
    resolve_import_path,
)
from src.knowledge import (
    delete_faq_vectors,
    sync_faq_ids,
    upsert_faq_ids,
)
from src.retrieve import invalidate_bm25_cache
from src.sensitive_store import (
    get_sensitive_store,
    load_patterns_from_path,
    load_patterns_from_url,
    resolve_sensitive_import_path,
)
from src.session import SqliteSessionStore

_bearer_scheme = HTTPBearer(
    auto_error=False,
    description="Authorization: Bearer <access_token>（先 POST /auth/login）",
)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="用户消息")
    session_id: str | None = Field(
        default=None,
        description="可选。不传则服务端自动生成；续聊时带上上次返回的 session_id",
    )


class ChatData(BaseModel):
    session_id: str = Field(description="会话 ID，续聊请原样回传")
    answer: str
    sources: list[str] = Field(default_factory=list)
    images: list[str] = Field(
        default_factory=list,
        description="配图绝对 URL，如 http://127.0.0.1:8000/assets/xxx.png",
    )
    clarify_options: list[str] = Field(default_factory=list)
    route: str = ""
    strategy: str = ""


class HealthData(BaseModel):
    status: str = "ok"


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, description="用户名")
    password: str = Field(..., min_length=1, description="密码")


class RegisterRequest(BaseModel):
    username: str = Field(
        ...,
        min_length=3,
        max_length=32,
        description="用户名（3–32 位字母数字下划线）",
    )
    password: str = Field(..., min_length=6, description="密码（至少 6 位）")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class TokenData(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
    refresh_token: str
    refresh_expires_in: int
    username: str = ""
    role: str = "user"


class SessionSummaryItem(BaseModel):
    session_id: str
    title: str
    preview: str = ""
    updated_at: int
    created_at: int


class SessionListData(BaseModel):
    items: list[SessionSummaryItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool


class SessionDetailData(BaseModel):
    session_id: str
    title: str
    messages: list[dict[str, str]]
    updated_at: int
    created_at: int


class SessionDeleteRequest(BaseModel):
    session_id: str = Field(..., min_length=1)


class UserItem(BaseModel):
    username: str
    role: str
    enabled: bool
    created_at: int


class UserListRequest(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=200)
    keyword: str | None = None
    role: str | None = None
    enabled: bool | None = None


class UserListData(BaseModel):
    items: list[UserItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    password: str = Field(..., min_length=6)
    role: str = Field("user", description="user 或 ops")


class UserUpdateRequest(BaseModel):
    username: str = Field(..., min_length=1)
    role: str | None = None
    enabled: bool | None = None
    password: str | None = Field(default=None, min_length=6)

    @model_validator(mode="after")
    def _at_least_one(self) -> UserUpdateRequest:
        if self.role is None and self.enabled is None and self.password is None:
            raise ValueError("至少需要更新一个字段")
        return self


class UserResetPasswordRequest(BaseModel):
    username: str = Field(..., min_length=1, description="目标用户名")
    new_password: str = Field(..., min_length=6, description="新密码（至少 6 位）")


class SessionClearData(BaseModel):
    status: str = "cleared"
    session_id: str


class FaqItem(BaseModel):
    id: str
    category: str = ""
    question: str
    answer: str
    similar: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: int
    updated_at: int


class FaqCreateRequest(BaseModel):
    question: str = Field(..., min_length=1)
    answer: str = Field(..., min_length=1)
    similar: list[str] = Field(default_factory=list)
    category: str = ""
    id: str | None = Field(default=None, description="可选自定义 ID")
    enabled: bool = True


class FaqListRequest(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=200)
    category: str | None = None
    keyword: str | None = None
    enabled: bool | None = None


class FaqListData(BaseModel):
    items: list[FaqItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool


class FaqGetRequest(BaseModel):
    id: str = Field(..., min_length=1)


class FaqUpdateRequest(BaseModel):
    id: str = Field(..., min_length=1)
    question: str | None = None
    answer: str | None = None
    similar: list[str] | None = None
    category: str | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> FaqUpdateRequest:
        if (
            self.question is None
            and self.answer is None
            and self.similar is None
            and self.category is None
            and self.enabled is None
        ):
            raise ValueError("至少需要更新一个字段")
        return self


class FaqDeleteRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)


class FaqImportRequest(BaseModel):
    url: str | None = Field(
        default=None,
        description="http(s) FAQ 文件 URL（json/csv/txt/xls/xlsx）",
    )
    path: str | None = Field(
        default=None,
        description="本地 FAQ 路径（相对项目根或绝对路径；json/csv/txt/xls/xlsx）",
    )

    @model_validator(mode="after")
    def _require_source(self) -> FaqImportRequest:
        url = (self.url or "").strip()
        path = (self.path or "").strip()
        if not url and not path:
            raise ValueError("须提供 url 或 path 之一")
        if url and path:
            raise ValueError("url 与 path 只能二选一")
        return self


class FaqImportData(BaseModel):
    imported: int
    total_in_file: int
    indexing: str = Field(
        default="async",
        description="async=向量后台同步；完成后对话即可命中新 FAQ",
    )


class FaqImportTemplateItem(BaseModel):
    format: str
    filename: str
    description: str


class FaqImportTemplateListData(BaseModel):
    items: list[FaqImportTemplateItem]


class BotScriptsData(BaseModel):
    welcome: str
    no_answer: str
    sensitive_reply: str
    handoff_reply: str
    handoff_keywords: list[str] = Field(default_factory=list)
    chitchat_reply: str
    chitchat_phrases: list[str] = Field(default_factory=list)


class BotScriptsUpdateRequest(BaseModel):
    welcome: str = Field(..., min_length=1)
    no_answer: str = Field(..., min_length=1)
    sensitive_reply: str = Field(..., min_length=1)
    handoff_reply: str = Field(..., min_length=1)
    handoff_keywords: list[str] = Field(default_factory=list)
    chitchat_reply: str = Field(..., min_length=1)
    chitchat_phrases: list[str] = Field(default_factory=list)


class SensitiveItem(BaseModel):
    id: str
    pattern: str
    enabled: bool = True
    note: str = ""
    created_at: int
    updated_at: int


class SensitiveCreateRequest(BaseModel):
    pattern: str = Field(..., min_length=1)
    note: str = ""
    enabled: bool = True
    id: str | None = Field(default=None, description="可选自定义 ID")


class SensitiveListRequest(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=200)
    keyword: str | None = None
    enabled: bool | None = None


class SensitiveListData(BaseModel):
    items: list[SensitiveItem]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_prev: bool


class SensitiveGetRequest(BaseModel):
    id: str = Field(..., min_length=1)


class SensitiveUpdateRequest(BaseModel):
    id: str = Field(..., min_length=1)
    pattern: str | None = None
    note: str | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> SensitiveUpdateRequest:
        if self.pattern is None and self.note is None and self.enabled is None:
            raise ValueError("至少需要更新一个字段")
        return self


class SensitiveDeleteRequest(BaseModel):
    ids: list[str] = Field(..., min_length=1)


class SensitiveImportRequest(BaseModel):
    url: str | None = Field(default=None, description="http(s) txt/json URL")
    path: str | None = Field(
        default=None,
        description="本地 txt/json 路径（相对项目根或绝对路径）",
    )

    @model_validator(mode="after")
    def _require_source(self) -> SensitiveImportRequest:
        url = (self.url or "").strip()
        path = (self.path or "").strip()
        if not url and not path:
            raise ValueError("须提供 url 或 path 之一")
        if url and path:
            raise ValueError("url 与 path 只能二选一")
        return self


class SensitiveImportData(BaseModel):
    imported: int
    skipped: int
    total_in_file: int


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _public_base_url(request: Request, settings: Settings) -> str:
    """Prefer ASSET_BASE_URL; otherwise use the request's public origin."""
    if settings.asset_base_url:
        return settings.asset_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _token_data(pair: Any) -> dict[str, Any]:
    return TokenData(
        access_token=pair.access_token,
        token_type=pair.token_type,
        expires_in=pair.expires_in,
        refresh_token=pair.refresh_token,
        refresh_expires_in=pair.refresh_expires_in,
        username=pair.username,
        role=pair.role,
    ).model_dump()


def create_api_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    bot = CustomerServiceBot(settings)
    store = SqliteSessionStore(settings.session_db_path)
    chat_logs = ChatLogStore(settings.chat_log_db_path)
    faq_store = FaqStore(settings.faq_db_path)
    sensitive_store = get_sensitive_store(settings.sensitive_db_path)
    auth = AuthService(
        db_path=settings.auth_db_path,
        jwt_secret=settings.jwt_secret,
        access_ttl=settings.jwt_access_ttl,
        refresh_ttl=settings.jwt_refresh_ttl,
        admin_username=settings.admin_username,
        admin_password=settings.admin_password,
        allow_register=settings.auth_allow_register,
    )
    bot_lock = threading.Lock()
    faq_lock = threading.Lock()
    sensitive_lock = threading.Lock()

    app = FastAPI(
        title="ReplyKit API",
        version="0.1.0",
        description=(
            "统一响应：JSON 接口均为 {code, message, data}；"
            "code 与 HTTP 状态一致（200 成功，4xx/500 失败）。"
            "鉴权：POST /auth/login 换 JWT（含 role）；"
            "Authorization: Bearer <access_token>。"
            "公开：/health、企微回调、/auth/login、/auth/register、/auth/refresh。"
            "普通用户：/chat、/sessions*；"
            "运营 ops：/faqs*、/sensitive-words*、/bot-scripts*、/users*。"
            "POST /chat；GET /sessions；"
            "POST /faqs、/faqs/list…；POST /users、/users/list、/users/update、/users/reset-password；"
            "GET/POST /bot-scripts*。"
        ),
    )
    # 本地 Vue(Vite) 直连 API 时用；开发默认走 Vite proxy，生产建议同源反向代理
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)

    def require_auth(
        bearer: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    ) -> dict[str, Any]:
        """Require a valid JWT access token in Authorization: Bearer."""
        if not bearer or not bearer.credentials:
            raise HTTPException(
                status_code=401,
                detail="未授权：请提供有效的 Bearer Token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return auth.decode_access(bearer.credentials.strip())

    def require_ops(
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        if str(user.get("role") or "") != "ops":
            raise HTTPException(status_code=403, detail="需要运营权限")
        return user

    @app.post("/auth/login", response_model=ApiResponse[TokenData])
    def auth_login(req: LoginRequest) -> dict[str, Any]:
        pair = auth.login(req.username, req.password)
        return ok(_token_data(pair))

    @app.post("/auth/register", response_model=ApiResponse[TokenData])
    def auth_register(req: RegisterRequest) -> dict[str, Any]:
        pair = auth.register(req.username, req.password)
        return ok(_token_data(pair), message="注册成功")

    @app.post("/auth/refresh", response_model=ApiResponse[TokenData])
    def auth_refresh(req: RefreshRequest) -> dict[str, Any]:
        pair = auth.refresh(req.refresh_token)
        return ok(_token_data(pair))

    @app.post("/auth/logout", response_model=ApiResponse[bool])
    def auth_logout(
        req: LogoutRequest,
        _: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        return ok(auth.logout(req.refresh_token), message="已退出")

    @app.post("/users/list", response_model=ApiResponse[UserListData])
    def users_list(
        req: UserListRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        result = auth.list_users(
            page=req.page,
            page_size=req.page_size,
            keyword=req.keyword,
            role=req.role,
            enabled=req.enabled,
        )
        data = UserListData(
            items=[
                UserItem(
                    username=u.username,
                    role=u.role,
                    enabled=u.enabled,
                    created_at=u.created_at,
                )
                for u in result.items
            ],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            total_pages=result.total_pages,
            has_next=result.has_next,
            has_prev=result.has_prev,
        )
        return ok(data.model_dump())

    @app.post("/users", response_model=ApiResponse[UserItem])
    def users_create(
        req: UserCreateRequest,
        actor: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        role = (req.role or "user").strip().lower()
        if role not in {"user", "ops"}:
            raise HTTPException(status_code=422, detail="role 须为 user 或 ops")
        user = auth.create_user(
            actor=str(actor["sub"]),
            username=req.username,
            password=req.password,
            role=role,  # type: ignore[arg-type]
        )
        return ok(
            UserItem(
                username=user.username,
                role=user.role,
                enabled=user.enabled,
                created_at=user.created_at,
            ).model_dump(),
            message="用户已创建",
        )

    @app.post("/users/update", response_model=ApiResponse[UserItem])
    def users_update(
        req: UserUpdateRequest,
        actor: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        role = req.role
        if role is not None:
            role = role.strip().lower()
            if role not in {"user", "ops"}:
                raise HTTPException(status_code=422, detail="role 须为 user 或 ops")
        user = auth.update_user(
            actor=str(actor["sub"]),
            username=req.username,
            role=role,  # type: ignore[arg-type]
            enabled=req.enabled,
            password=req.password,
        )
        return ok(
            UserItem(
                username=user.username,
                role=user.role,
                enabled=user.enabled,
                created_at=user.created_at,
            ).model_dump(),
            message="用户已更新",
        )

    @app.post("/users/reset-password", response_model=ApiResponse[UserItem])
    def users_reset_password(
        req: UserResetPasswordRequest,
        actor: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        user = auth.reset_password(
            actor=str(actor["sub"]),
            username=req.username,
            new_password=req.new_password,
        )
        return ok(
            UserItem(
                username=user.username,
                role=user.role,
                enabled=user.enabled,
                created_at=user.created_at,
            ).model_dump(),
            message="密码已重置，请通知用户使用新密码登录",
        )

    @app.get("/sessions", response_model=ApiResponse[SessionListData])
    def list_sessions(
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        result = store.list_for_user(
            str(user["sub"]), page=page, page_size=page_size
        )
        data = SessionListData(
            items=[
                SessionSummaryItem(
                    session_id=s.session_id,
                    title=s.title,
                    preview=s.preview,
                    updated_at=s.updated_at,
                    created_at=s.created_at,
                )
                for s in result.items
            ],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            total_pages=result.total_pages,
            has_next=result.has_next,
            has_prev=result.has_prev,
        )
        return ok(data.model_dump())

    @app.get(
        "/sessions/{session_id}",
        response_model=ApiResponse[SessionDetailData],
    )
    def get_session(
        session_id: str,
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        owned = store.get_for_user(session_id, str(user["sub"]))
        if owned is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return ok(
            SessionDetailData(
                session_id=session_id,
                title=owned.title or "新会话",
                messages=list(owned.history),
                updated_at=owned.updated_at,
                created_at=owned.created_at,
            ).model_dump()
        )

    @app.post("/sessions/delete", response_model=ApiResponse[bool])
    def delete_session(
        req: SessionDeleteRequest,
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        ok_del = store.delete_for_user(req.session_id, str(user["sub"]))
        if not ok_del:
            raise HTTPException(status_code=404, detail="会话不存在")
        return ok(True, message="已删除")

    @app.get("/health", response_model=ApiResponse[HealthData])
    def health() -> dict[str, Any]:
        return ok(HealthData(status="ok").model_dump())

    @app.get("/webhooks/wecom")
    def wecom_verify(
        msg_signature: str = Query(...),
        timestamp: str = Query(...),
        nonce: str = Query(...),
        echostr: str = Query(...),
    ) -> Response:
        """企微 URL 校验：返回解密后的 echostr（纯文本，非 JSON）。"""
        if not wecom_configured(settings):
            raise HTTPException(status_code=503, detail="企业微信渠道未启用或未配置完整")
        # 部分网关会把 query 里 base64 的 '+' 变成空格，导致验签/解密失败
        echostr = echostr.replace(" ", "+")
        try:
            plain = verify_url_echo(
                settings,
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
                echostr=echostr,
            )
        except WeComCryptoError as exc:
            logging.getLogger(__name__).warning(
                "wecom verify failed: %s (code=%s, echostr_len=%s)",
                exc,
                exc.code,
                len(echostr),
            )
            raise HTTPException(status_code=403, detail="企微签名或解密失败") from exc
        logging.getLogger(__name__).info("wecom URL verify ok")
        return Response(content=plain, media_type="text/plain; charset=utf-8")

    @app.post("/webhooks/wecom")
    async def wecom_callback(
        request: Request,
        msg_signature: str = Query(...),
        timestamp: str = Query(...),
        nonce: str = Query(...),
    ) -> Response:
        """企微消息回调：解密 → 复用 bot → 加密 XML 被动回复。"""
        if not wecom_configured(settings):
            raise HTTPException(status_code=503, detail="企业微信渠道未启用或未配置完整")

        body = (await request.body()).decode("utf-8", errors="replace")
        try:
            incoming = decrypt_post(
                settings,
                body=body,
                msg_signature=msg_signature,
                timestamp=timestamp,
                nonce=nonce,
            )
        except WeComCryptoError as exc:
            logging.getLogger(__name__).warning("wecom decrypt failed: %s", exc)
            raise HTTPException(status_code=403, detail="企微签名或解密失败") from exc

        if incoming.msg_type != "text" or not incoming.content:
            reply_text = settings.wecom_unsupported_msg_reply
        else:
            session_id = session_id_for_user(settings, incoming.from_user)
            try:
                session = store.get(session_id)
                with bot_lock:
                    bot.load_session(session.bot_state)
                    result = bot.chat_result(
                        incoming.content, history=session.history
                    )
                    display_user = (
                        bot.last_effective_query or incoming.content
                    ).strip()
                    history = list(session.history) + [
                        {"role": "user", "content": display_user},
                        {"role": "assistant", "content": result.answer},
                    ]
                    store.save(
                        session_id,
                        history=history,
                        bot_state=bot.dump_session(),
                        username=f"wecom:{incoming.from_user}",
                    )
                    log_turn_safe(
                        chat_logs,
                        session_id=session_id,
                        question=display_user,
                        result=result,
                        username=f"wecom:{incoming.from_user}",
                    )
                images = paths_to_asset_urls(
                    result.images,
                    settings.assets_dir,
                    base_url=_public_base_url(request, settings),
                )
                reply_text = format_bot_answer(
                    result.answer,
                    clarify_options=list(result.clarify_options),
                    images=images,
                )
            except Exception:  # noqa: BLE001 - 避免企微重试风暴
                logging.getLogger(__name__).exception("wecom bot failed")
                reply_text = "系统繁忙，请稍后再试。"

        try:
            encrypted = encrypt_text_reply(
                settings,
                to_user=incoming.from_user,
                content=reply_text,
                nonce=nonce,
                timestamp=timestamp,
            )
        except WeComCryptoError:
            logging.getLogger(__name__).exception("wecom encrypt reply failed")
            return Response(content="", media_type="text/plain")
        return Response(content=encrypted, media_type="application/xml")

    @app.post("/chat", response_model=ApiResponse[ChatData])
    def chat(
        req: ChatRequest,
        request: Request,
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        message = req.message.strip()
        if not message:
            raise HTTPException(status_code=422, detail="message 不能为空")

        username = str(user["sub"])
        raw_sid = (req.session_id or "").strip()
        if raw_sid:
            owned = store.get_for_user(raw_sid, username)
            if owned is None:
                # 已存在但非本人
                existing = store.get(raw_sid)
                if existing.username and existing.username != username:
                    raise HTTPException(status_code=403, detail="无权使用该会话")
                if existing.username is None and existing.history:
                    # 无主且已有内容的旧会话，禁止接管
                    raise HTTPException(status_code=403, detail="无权使用该会话")
                session = existing
                session_id = raw_sid
            else:
                session = owned
                session_id = raw_sid
        else:
            session_id = _new_session_id()
            session = store.get(session_id)

        with bot_lock:
            bot.load_session(session.bot_state)
            result = bot.chat_result(message, history=session.history)
            display_user = (bot.last_effective_query or message).strip()
            history = list(session.history) + [
                {"role": "user", "content": display_user},
                {"role": "assistant", "content": result.answer},
            ]
            store.save(
                session_id,
                history=history,
                bot_state=bot.dump_session(),
                username=username,
            )
            log_turn_safe(
                chat_logs,
                session_id=session_id,
                question=display_user,
                result=result,
                username=username,
            )

        images = paths_to_asset_urls(
            result.images,
            settings.assets_dir,
            base_url=_public_base_url(request, settings),
        )
        data = ChatData(
            session_id=session_id,
            answer=result.answer,
            sources=list(result.sources),
            images=images,
            clarify_options=list(result.clarify_options),
            route=result.route,
            strategy=result.strategy,
        )
        return ok(data.model_dump())

    # ---- FAQ management (P3-3): all POST ----

    @app.post("/faqs/list", response_model=ApiResponse[FaqListData])
    def faq_list(
        req: FaqListRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        result = faq_store.list_page(
            page=req.page,
            page_size=req.page_size,
            category=req.category,
            keyword=req.keyword,
            enabled=req.enabled,
        )
        data = FaqListData(
            items=[FaqItem(**row.to_dict()) for row in result.items],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            total_pages=result.total_pages,
            has_next=result.has_next,
            has_prev=result.has_prev,
        )
        return ok(data.model_dump())

    @app.post("/faqs/get", response_model=ApiResponse[FaqItem])
    def faq_get(
        req: FaqGetRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        row = faq_store.get(req.id.strip())
        if row is None:
            raise HTTPException(status_code=404, detail="FAQ 不存在")
        return ok(FaqItem(**row.to_dict()).model_dump())

    @app.post("/faqs", response_model=ApiResponse[FaqItem])
    def faq_create(
        req: FaqCreateRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            with faq_lock:
                row = faq_store.create(
                    question=req.question,
                    answer=req.answer,
                    similar=req.similar,
                    category=req.category,
                    faq_id=req.id,
                    enabled=req.enabled,
                )
                faq_id = row.id
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        invalidate_bm25_cache()
        _schedule_faq_vector_sync([faq_id])
        return ok(FaqItem(**row.to_dict()).model_dump(), message="创建成功")

    @app.post("/faqs/update", response_model=ApiResponse[FaqItem])
    def faq_update(
        req: FaqUpdateRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            with faq_lock:
                row = faq_store.update(
                    req.id.strip(),
                    question=req.question,
                    answer=req.answer,
                    similar=req.similar,
                    category=req.category,
                    enabled=req.enabled,
                )
                faq_id = row.id
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="FAQ 不存在") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        invalidate_bm25_cache()
        _schedule_faq_vector_sync([faq_id])
        return ok(FaqItem(**row.to_dict()).model_dump(), message="更新成功")

    @app.post("/faqs/delete", response_model=ApiResponse[bool])
    def faq_delete(
        req: FaqDeleteRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            with faq_lock:
                deleted = faq_store.delete_many(req.ids)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        invalidate_bm25_cache()
        if deleted:
            _schedule_faq_vector_delete(deleted)
        return ok(True, message="删除成功")

    @app.post("/faqs/import", response_model=ApiResponse[FaqImportData])
    def faq_import(
        req: FaqImportRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            if req.url:
                entries = load_faq_entries_from_url(req.url.strip())
            else:
                path = resolve_import_path(req.path or "")
                entries = load_faq_entries_from_path(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return _faq_import_entries(entries)

    @app.post("/faqs/import-file", response_model=ApiResponse[FaqImportData])
    async def faq_import_file(
        file: UploadFile = File(..., description="FAQ 文件（json/csv/txt/xls/xlsx）"),
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        filename = (file.filename or "").strip()
        fmt = detect_format(filename)
        if not fmt:
            raise HTTPException(
                status_code=422,
                detail=(
                    "无法识别文件格式，请上传："
                    ".json / .csv / .txt / .xls / .xlsx"
                ),
            )
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=422, detail="上传文件为空")
        try:
            entries = parse_faq_bytes(raw, fmt)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not entries:
            raise HTTPException(
                status_code=422,
                detail="文件中没有有效的 FAQ（需含 question/answer 或 Q:/A:）",
            )
        return _faq_import_entries(entries)

    def _schedule_faq_vector_sync(faq_ids: list[str]) -> None:
        """DB 已落库后异步重建向量，避免创建/更新接口卡住等 embedding。"""
        cleaned = [str(i).strip() for i in faq_ids if str(i).strip()]
        if not cleaned:
            return

        def _run() -> None:
            try:
                sync_faq_ids(settings, cleaned)
            except Exception:
                logging.getLogger(__name__).exception(
                    "FAQ background vector sync failed for %s",
                    cleaned,
                )

        threading.Thread(
            target=_run,
            name="faq-vector-sync",
            daemon=True,
        ).start()

    def _schedule_faq_vector_delete(faq_ids: list[str]) -> None:
        cleaned = [str(i).strip() for i in faq_ids if str(i).strip()]
        if not cleaned:
            return

        def _run() -> None:
            try:
                delete_faq_vectors(settings, cleaned)
            except Exception:
                logging.getLogger(__name__).exception(
                    "FAQ background vector delete failed for %s",
                    cleaned,
                )

        threading.Thread(
            target=_run,
            name="faq-vector-delete",
            daemon=True,
        ).start()

    def _faq_import_entries(entries: list) -> dict[str, Any]:
        with faq_lock:
            result = faq_store.import_entries(entries)
            touched_ids = list(result.touched_ids)

        if touched_ids:
            def _index_imported() -> None:
                try:
                    upsert_faq_ids(settings, touched_ids)
                except Exception:
                    logging.getLogger(__name__).exception(
                        "FAQ import background indexing failed for %s",
                        touched_ids,
                    )

            threading.Thread(
                target=_index_imported,
                name="faq-import-index",
                daemon=True,
            ).start()

        data = FaqImportData(
            imported=result.imported,
            total_in_file=result.total_in_file,
            indexing="async",
        )
        invalidate_bm25_cache()
        return ok(data.model_dump(), message="导入成功")

    @app.get(
        "/faqs/import-templates",
        response_model=ApiResponse[FaqImportTemplateListData],
    )
    def faq_import_templates_list(
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        items = [FaqImportTemplateItem(**m) for m in list_template_meta()]
        return ok(FaqImportTemplateListData(items=items).model_dump())

    @app.get("/faqs/import-templates/{template_format}")
    def faq_import_template_download(
        template_format: str,
        _: dict[str, Any] = Depends(require_ops),
    ) -> Response:
        fmt = (template_format or "").strip().lower()
        if fmt == "text":
            fmt = "txt"
        if fmt not in SUPPORTED_FORMATS:
            raise HTTPException(
                status_code=422,
                detail=f"format 须为：{', '.join(SUPPORTED_FORMATS)}",
            )
        try:
            content, filename, media_type = build_template_file(fmt)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.get("/bot-scripts", response_model=ApiResponse[BotScriptsData])
    def bot_scripts_get(
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            data = get_bot_scripts()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(BotScriptsData(**data).model_dump())

    @app.get("/bot-scripts/template", response_model=ApiResponse[BotScriptsData])
    def bot_scripts_template(
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            data = load_bot_scripts_template()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(BotScriptsData(**data).model_dump(), message="内置模板")

    @app.post("/bot-scripts/update", response_model=ApiResponse[BotScriptsData])
    def bot_scripts_update(
        req: BotScriptsUpdateRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            data = save_bot_scripts(req.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(
            BotScriptsData(**data).model_dump(),
            message="话术已保存（热更新，无需重启）",
        )

    @app.post("/bot-scripts/reset-template", response_model=ApiResponse[BotScriptsData])
    def bot_scripts_reset_template(
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            data = reset_bot_scripts_from_template()
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(
            BotScriptsData(**data).model_dump(),
            message="已用模板初始化话术（热更新，无需重启）",
        )

    @app.post("/sensitive-words/list", response_model=ApiResponse[SensitiveListData])
    def sensitive_list(
        req: SensitiveListRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        result = sensitive_store.list_page(
            page=req.page,
            page_size=req.page_size,
            keyword=req.keyword,
            enabled=req.enabled,
        )
        data = SensitiveListData(
            items=[SensitiveItem(**row.to_dict()) for row in result.items],
            total=result.total,
            page=result.page,
            page_size=result.page_size,
            total_pages=result.total_pages,
            has_next=result.has_next,
            has_prev=result.has_prev,
        )
        return ok(data.model_dump())

    @app.post("/sensitive-words/get", response_model=ApiResponse[SensitiveItem])
    def sensitive_get(
        req: SensitiveGetRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        row = sensitive_store.get(req.id.strip())
        if row is None:
            raise HTTPException(status_code=404, detail="敏感词不存在")
        return ok(SensitiveItem(**row.to_dict()).model_dump())

    @app.post("/sensitive-words", response_model=ApiResponse[SensitiveItem])
    def sensitive_create(
        req: SensitiveCreateRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            with sensitive_lock:
                row = sensitive_store.create(
                    pattern=req.pattern,
                    note=req.note,
                    enabled=req.enabled,
                    word_id=req.id,
                )
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(SensitiveItem(**row.to_dict()).model_dump(), message="创建成功")

    @app.post("/sensitive-words/update", response_model=ApiResponse[SensitiveItem])
    def sensitive_update(
        req: SensitiveUpdateRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            with sensitive_lock:
                row = sensitive_store.update(
                    req.id.strip(),
                    pattern=req.pattern,
                    note=req.note,
                    enabled=req.enabled,
                )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except LookupError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(SensitiveItem(**row.to_dict()).model_dump(), message="更新成功")

    @app.post("/sensitive-words/delete", response_model=ApiResponse[bool])
    def sensitive_delete(
        req: SensitiveDeleteRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            with sensitive_lock:
                sensitive_store.delete_many(req.ids)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ok(True, message="删除成功")

    @app.post("/sensitive-words/import", response_model=ApiResponse[SensitiveImportData])
    def sensitive_import(
        req: SensitiveImportRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            if req.url:
                patterns = load_patterns_from_url(req.url.strip())
            else:
                path = resolve_sensitive_import_path(req.path or "")
                patterns = load_patterns_from_path(path)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        with sensitive_lock:
            result = sensitive_store.import_patterns(patterns)
        return ok(
            SensitiveImportData(
                imported=result.imported,
                skipped=result.skipped,
                total_in_file=result.total_in_file,
            ).model_dump(),
            message=f"导入完成：新增 {result.imported}，跳过 {result.skipped}",
        )

    @app.delete("/sessions/{session_id}", response_model=ApiResponse[SessionClearData])
    def clear_session_legacy(
        session_id: str,
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        if not store.delete_for_user(session_id, str(user["sub"])):
            raise HTTPException(status_code=404, detail="会话不存在")
        return ok(
            SessionClearData(status="cleared", session_id=session_id).model_dump()
        )

    # Mount after routes so /chat、/health 不被静态路由抢走。
    # 不在启动时自动创建 assets；目录不存在时仍挂载（check_dir=False），请求时 404。
    assets_dir = settings.assets_dir
    app.mount(
        "/assets",
        StaticFiles(directory=str(assets_dir), check_dir=False),
        name="assets",
    )

    return app


# uvicorn src.api:app
app = create_api_app()


def main() -> None:
    import uvicorn

    host = os.getenv("API_HOST", "127.0.0.1")
    port = int(os.getenv("API_PORT", "8000"))
    uvicorn.run(
        "src.api:app",
        host=host,
        port=port,
        reload=False,
    )


if __name__ == "__main__":
    main()
