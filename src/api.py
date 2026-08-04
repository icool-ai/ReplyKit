"""FastAPI HTTP surface for the customer service bot (P2).

Share the same ``CustomerServiceBot`` logic as the Vue front-end / external clients.
Sessions persist in SQLite (``SESSION_DB_PATH``).
Images are exposed as ``/assets/...`` URLs (P2-4).
Auth: JWT Bearer after ``POST /auth/login`` (no X-API-Key).
Dify external knowledge: ``POST /retrieval`` authenticates against keys from
ops ``GET/POST /integrations/dify/keys*`` and returns Dify-native JSON.

JSON responses use a unified envelope: ``{code, message, data}``.
File download responses (e.g. FAQ templates) and Dify ``/retrieval`` remain
raw / protocol-specific.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import uuid
from typing import Any

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Security,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
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
from src.channel_store import AppIdTakenError, ChannelConfigRow, get_channel_store
from src.channels.feishu import (
    FeishuCryptoError,
    extract_text as feishu_extract_text,
    feishu_can_reply,
    feishu_ready,
    feishu_setup_hint,
    format_bot_answer as format_feishu_answer,
    parse_event_body as parse_feishu_event_body,
    remember_message as remember_feishu_message,
    reply_text as feishu_reply_text,
    session_id_for as feishu_session_id,
    verify_app_credentials,
    verify_signature as verify_feishu_signature,
)
from src.channels.feishu_oauth import (
    OAUTH_CALLBACK_PATH,
    consume_oauth_state,
    exchange_code_for_token,
    html_oauth_page,
    oauth_redirect_uri,
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
from src.dify_retrieval import (
    dify_error,
    docs_to_records,
)
from src.skills.base import ChannelContext
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
    search_faq,
    sync_faq_ids,
    upsert_faq_ids,
    vectorstore_exists,
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


class FeishuChannelData(BaseModel):
    id: str | None = None
    owner_username: str
    channel: str = "feishu"
    enabled: bool = False
    app_id: str = ""
    app_secret_set: bool = False
    verification_token_set: bool = False
    encrypt_key_set: bool = False
    callback_path: str | None = None
    created_at: int | None = None
    updated_at: int | None = None


class FeishuChannelUpdateRequest(BaseModel):
    enabled: bool
    app_id: str = ""
    app_secret: str | None = Field(
        default=None,
        description="留空或不传则保留原值；首次启用必填",
    )
    verification_token: str | None = Field(
        default=None,
        description="留空或不传则保留原值；首次启用必填",
    )
    encrypt_key: str | None = Field(
        default=None,
        description="留空或不传则保留原值",
    )


class DifyApiKeyItem(BaseModel):
    id: str
    name: str
    endpoint: str
    knowledge_id: str
    api_key_masked: str
    api_key_set: bool = True
    created_at: int
    updated_at: int
    last_used_at: int | None = None
    api_key: str | None = Field(
        default=None,
        description="仅新建时返回一次明文",
    )


class DifyApiKeyListData(BaseModel):
    items: list[DifyApiKeyItem]
    retrieval_path: str = "/retrieval"


class DifyApiKeyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, description="配置名称")
    endpoint: str = Field(
        ...,
        min_length=1,
        description="填到 Dify 的公网根地址（不要带 /retrieval）",
    )
    knowledge_id: str = Field(
        default="faq",
        description="外部知识库 ID；与 Dify 连接时填写的一致",
    )


class DifyApiKeyUpdateRequest(BaseModel):
    name: str | None = None
    endpoint: str | None = None
    knowledge_id: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> DifyApiKeyUpdateRequest:
        if self.name is None and self.endpoint is None and self.knowledge_id is None:
            raise ValueError("至少需要更新一个字段")
        return self


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
    channel_store = get_channel_store(settings.channels_db_path)
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
    channel_lock = threading.Lock()

    def _empty_feishu(owner: str) -> dict[str, Any]:
        return FeishuChannelData(
            owner_username=owner,
            callback_path="/webhooks/feishu",
        ).model_dump()

    def _feishu_public(row: Any) -> dict[str, Any]:
        return FeishuChannelData(**row.to_public_dict()).model_dump()

    app = FastAPI(
        title="ReplyKit API",
        version="0.1.0",
        description=(
            "统一响应：JSON 接口均为 {code, message, data}；"
            "code 与 HTTP 状态一致（200 成功，4xx/500 失败）。"
            "鉴权：POST /auth/login 换 JWT（含 role）；"
            "Authorization: Bearer <access_token>。"
            "公开：/health、企微/飞书回调、飞书 OAuth 回调、"
            "/auth/login、/auth/register、/auth/refresh；"
            "POST /retrieval（Dify 外部知识库，Bearer=平台配置的 API Key，原生 JSON）。"
            "普通用户：/chat、/sessions*、GET /bot-scripts、/channels/feishu*；"
            "运营 ops：/faqs*、/sensitive-words*、POST /bot-scripts*、/users*、"
            "GET/POST /integrations/dify/keys*。"
            "POST /chat；GET /sessions；"
            "POST /faqs、/faqs/list…；POST /users、/users/list、/users/update、/users/reset-password；"
            "GET /bot-scripts（登录可读）；POST /bot-scripts*（ops）；"
            "GET/POST /channels/feishu*（登录用户各自隔离；App ID 独占）；"
            "POST /webhooks/feishu（公开，飞书事件，无 JWT；按 App ID/Token 匹配配置）；"
            f"GET {OAUTH_CALLBACK_PATH}（公开，飞书用户授权回调，返回 HTML）。"
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

    @app.post("/retrieval")
    async def dify_retrieval(
        request: Request,
        bearer: HTTPAuthorizationCredentials | None = Security(_bearer_scheme),
    ) -> JSONResponse:
        """Dify 外部知识库检索：原生 ``{"records":[...]}``，不用统一信封。"""
        if not bearer or not bearer.credentials:
            return JSONResponse(
                status_code=401,
                content=dify_error(
                    error_code=1001,
                    error_msg="无效的 Authorization 请求头格式",
                ),
                headers={"WWW-Authenticate": "Bearer"},
            )
        provided = bearer.credentials.strip()
        key_row = channel_store.find_dify_api_key_by_secret(provided)
        if key_row is None:
            # 区分「未配置任何 Key」与「Key 错误」
            if not channel_store.list_dify_api_keys():
                return JSONResponse(
                    status_code=401,
                    content=dify_error(
                        error_code=1002,
                        error_msg="未在平台配置 Dify API Key（渠道配置 → Dify）",
                    ),
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return JSONResponse(
                status_code=401,
                content=dify_error(
                    error_code=1002,
                    error_msg="认证失败，请检查 API Key",
                ),
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content=dify_error(
                    error_code=2002,
                    error_msg="请求体须为 JSON",
                ),
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content=dify_error(
                    error_code=2002,
                    error_msg="请求体须为 JSON 对象",
                ),
            )

        knowledge_id = str(body.get("knowledge_id") or "").strip()
        query = str(body.get("query") or "").strip()
        retrieval_setting = body.get("retrieval_setting")
        if not isinstance(retrieval_setting, dict):
            return JSONResponse(
                status_code=400,
                content=dify_error(
                    error_code=2002,
                    error_msg="缺少必填字段 retrieval_setting",
                ),
            )
        # Dify 保存 API 时会发 knowledge_id="" / query="" 探测；须 200 + records
        if not query:
            return JSONResponse(status_code=200, content={"records": []})
        if not knowledge_id:
            return JSONResponse(
                status_code=400,
                content=dify_error(
                    error_code=2002,
                    error_msg="缺少必填字段 knowledge_id",
                ),
            )
        if knowledge_id != key_row.knowledge_id:
            return JSONResponse(
                status_code=404,
                content=dify_error(
                    error_code=2001,
                    error_msg="知识库不存在或与该 API Key 不匹配",
                ),
            )

        try:
            top_k = int(retrieval_setting.get("top_k"))
            score_threshold = float(retrieval_setting.get("score_threshold"))
        except (TypeError, ValueError):
            return JSONResponse(
                status_code=400,
                content=dify_error(
                    error_code=2002,
                    error_msg="retrieval_setting.top_k / score_threshold 无效",
                ),
            )
        if top_k < 1:
            top_k = 1
        if score_threshold < 0:
            score_threshold = 0.0

        if not vectorstore_exists(settings):
            return JSONResponse(
                status_code=500,
                content=dify_error(
                    error_code=5000,
                    error_msg="向量库未初始化，请先导入 FAQ 并重建索引",
                ),
            )

        try:
            docs, _cands, _route = search_faq(settings, query, k=top_k)
            records = docs_to_records(
                docs,
                score_threshold=score_threshold,
                top_k=top_k,
            )
            channel_store.touch_dify_api_key_used(key_row.id)
        except Exception as exc:  # noqa: BLE001 — surface to Dify as protocol error
            logging.getLogger(__name__).exception("dify /retrieval failed")
            return JSONResponse(
                status_code=500,
                content=dify_error(
                    error_code=5000,
                    error_msg=f"检索失败：{exc}",
                ),
            )
        return JSONResponse(status_code=200, content={"records": records})

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
                    bot.channel_ctx = ChannelContext(channel="wecom")
                    try:
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
                    finally:
                        bot.channel_ctx = None
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

    def _feishu_handle_message(
        *,
        config_id: str,
        app_id: str,
        app_secret: str,
        event: dict[str, Any],
        public_base: str,
    ) -> None:
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        msg_type = (message.get("message_type") or "").lower()
        message_id = message.get("message_id") or ""
        if not message_id:
            return
        dedupe = f"{config_id}:{message_id}"
        if not remember_feishu_message(dedupe):
            logging.getLogger(__name__).info(
                "feishu skip duplicate config=%s message_id=%s",
                config_id,
                message_id,
            )
            return

        chat_type = (message.get("chat_type") or "").lower()
        if chat_type == "group":
            mentions = message.get("mentions") or []
            if not mentions:
                logging.getLogger(__name__).info(
                    "feishu skip group without @bot config=%s message_id=%s",
                    config_id,
                    message_id,
                )
                return

        def _reply(text: str) -> None:
            feishu_reply_text(
                app_id=app_id,
                app_secret=app_secret,
                message_id=message_id,
                text=text,
            )

        if msg_type != "text":
            _reply("当前仅支持文字消息，请直接输入问题。")
            return

        text = feishu_extract_text(message.get("content") or "")
        if not text:
            return

        open_id = ((sender.get("sender_id") or {}).get("open_id") or "").strip()
        session_id = feishu_session_id(config_id, open_id or message_id)
        username = f"feishu:{config_id}:{open_id or message_id}"
        try:
            session = store.get(session_id)
            with bot_lock:
                bot.load_session(session.bot_state)
                bot.channel_ctx = ChannelContext(
                    channel="feishu",
                    open_id=open_id,
                    feishu_config_id=config_id,
                    app_id=app_id,
                    app_secret=app_secret,
                    public_base=public_base,
                )
                try:
                    result = bot.chat_result(text, history=session.history)
                    display_user = (bot.last_effective_query or text).strip()
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
                finally:
                    bot.channel_ctx = None
            images = paths_to_asset_urls(
                result.images,
                settings.assets_dir,
                base_url=public_base,
            )
            answer = format_feishu_answer(
                result.answer,
                clarify_options=list(result.clarify_options),
                images=images,
            )
            _reply(answer)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "feishu bot failed config=%s", config_id
            )
            try:
                _reply("系统繁忙，请稍后再试。")
            except Exception:  # noqa: BLE001
                logging.getLogger(__name__).exception(
                    "feishu error reply failed config=%s", config_id
                )

    def _feishu_reply_hint(
        *,
        app_id: str,
        app_secret: str,
        message_id: str,
        hint: str,
    ) -> None:
        if not message_id or not app_id or not app_secret:
            return
        try:
            feishu_reply_text(
                app_id=app_id,
                app_secret=app_secret,
                message_id=message_id,
                text=hint,
            )
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "feishu setup hint reply failed app_id=%s", app_id
            )

    def _resolve_feishu_row(
        raw: bytes,
        *,
        fixed_row: ChannelConfigRow | None = None,
        timestamp: str | None = None,
        nonce: str | None = None,
        signature: str | None = None,
    ) -> tuple[ChannelConfigRow, dict[str, Any]]:
        try:
            outer = json.loads(raw.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail="请求体不是合法 JSON，请检查飞书回调是否指向正确地址",
            ) from exc

        all_rows = (
            [fixed_row]
            if fixed_row is not None
            else channel_store.list_feishu()
        )
        if not all_rows:
            raise HTTPException(
                status_code=503,
                detail=(
                    "尚未在 ReplyKit「渠道配置」中保存飞书凭证。"
                    "请先登录前端填写 App ID 等并启用，再保存飞书请求地址。"
                ),
            )

        if "encrypt" in outer:
            for row in all_rows:
                if not row.encrypt_key:
                    continue
                if signature:
                    if not (
                        timestamp
                        and nonce
                        and verify_feishu_signature(
                            row.encrypt_key, timestamp, nonce, raw, signature
                        )
                    ):
                        continue
                try:
                    body = parse_feishu_event_body(raw, row.encrypt_key)
                except FeishuCryptoError:
                    continue
                header = body.get("header") or {}
                app_id = str(header.get("app_id") or "").strip()
                if app_id and row.app_id and app_id != row.app_id:
                    continue
                token = str(body.get("token") or header.get("token") or "").strip()
                if (
                    token
                    and row.verification_token
                    and token != row.verification_token
                ):
                    continue
                return row, body
            raise HTTPException(
                status_code=401,
                detail=(
                    "无法用已保存的 Encrypt Key 解密/验签。"
                    "请确认 ReplyKit「渠道配置」中的 Encrypt Key、"
                    "Verification Token 与飞书后台一致，并已保存。"
                ),
            )

        body = outer
        header = body.get("header") or {}
        app_id = str(header.get("app_id") or "").strip()
        token = str(body.get("token") or header.get("token") or "").strip()

        row: ChannelConfigRow | None = None
        if fixed_row is not None:
            row = fixed_row
        elif app_id:
            row = channel_store.find_by_app_id(app_id)
        if row is None and token:
            row = channel_store.find_by_verification_token(token)
        if row is None:
            ready = [r for r in all_rows if feishu_ready(r)]
            if len(ready) == 1:
                row = ready[0]
            elif len(all_rows) == 1:
                row = all_rows[0]
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "未匹配到飞书渠道配置。"
                    "请确认页面已保存对应 App ID，且飞书事件来自该应用。"
                ),
            )

        if row.encrypt_key and signature:
            if not (
                timestamp
                and nonce
                and verify_feishu_signature(
                    row.encrypt_key, timestamp, nonce, raw, signature
                )
            ):
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "签名校验失败。请检查 Encrypt Key 是否与飞书后台一致。"
                    ),
                )
        if token and row.verification_token and token != row.verification_token:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Verification Token 不匹配。"
                    "请核对 ReplyKit「渠道配置」与飞书「加密策略」中的 Token。"
                ),
            )
        return row, body

    async def _dispatch_feishu_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        *,
        fixed_row: ChannelConfigRow | None = None,
        x_lark_request_timestamp: str | None = None,
        x_lark_request_nonce: str | None = None,
        x_lark_signature: str | None = None,
    ) -> dict[str, Any]:
        raw = await request.body()
        row, body = _resolve_feishu_row(
            raw,
            fixed_row=fixed_row,
            timestamp=x_lark_request_timestamp,
            nonce=x_lark_request_nonce,
            signature=x_lark_signature,
        )
        hint = feishu_setup_hint(row)

        if body.get("type") == "url_verification" or body.get("challenge"):
            token = body.get("token") or ""
            if row.verification_token and token != row.verification_token:
                raise HTTPException(
                    status_code=401,
                    detail=(
                        "Verification Token 不匹配，URL 校验失败。"
                        "请在 ReplyKit「渠道配置」填写与飞书后台一致的 Token。"
                    ),
                )
            if not feishu_ready(row):
                # 仍返回 challenge，方便管理员先通过 URL 校验；同时用 detail 打日志
                logging.getLogger(__name__).warning(
                    "feishu url_verification ok but config not ready owner=%s: %s",
                    row.owner_username,
                    hint,
                )
            return {"challenge": body["challenge"]}

        header = body.get("header") or {}
        token = body.get("token") or header.get("token") or ""
        if row.verification_token and token and token != row.verification_token:
            raise HTTPException(
                status_code=401,
                detail=(
                    "Verification Token 不匹配。"
                    "请核对 ReplyKit「渠道配置」与飞书后台。"
                ),
            )

        event_type = header.get("event_type") or body.get("type") or ""
        if event_type == "im.message.receive_v1":
            event = body.get("event") or {}
            message = event.get("message") or {}
            message_id = str(message.get("message_id") or "")

            if not feishu_ready(row):
                logging.getLogger(__name__).warning(
                    "feishu message but config not ready owner=%s: %s",
                    row.owner_username,
                    hint,
                )
                chat_type = str((message.get("chat_type") or "")).lower()
                if chat_type == "group" and not (message.get("mentions") or []):
                    return {"code": 0}
                if feishu_can_reply(row) and message_id:
                    background_tasks.add_task(
                        _feishu_reply_hint,
                        app_id=row.app_id,
                        app_secret=row.app_secret,
                        message_id=message_id,
                        hint=hint,
                    )
                    return {"code": 0}
                raise HTTPException(status_code=503, detail=hint)

            public_base = _public_base_url(request, settings)
            background_tasks.add_task(
                _feishu_handle_message,
                config_id=row.id,
                app_id=row.app_id,
                app_secret=row.app_secret,
                event=event,
                public_base=public_base,
            )
        else:
            logging.getLogger(__name__).info(
                "feishu ignore event_type=%s owner=%s",
                event_type,
                row.owner_username,
            )
        return {"code": 0}

    @app.get(OAUTH_CALLBACK_PATH, response_class=HTMLResponse)
    def feishu_oauth_callback(
        request: Request,
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
    ) -> HTMLResponse:
        """飞书用户授权回调（公开 HTML）：用 code 换 user_access_token 并落库。"""
        if error:
            return HTMLResponse(
                content=html_oauth_page(
                    title="授权已取消",
                    message="你已取消飞书授权。请回到飞书，重新对机器人说查询任务以获取新链接。",
                    ok=False,
                ),
                status_code=400,
            )
        if not code or not state:
            return HTMLResponse(
                content=html_oauth_page(
                    title="授权失败",
                    message="缺少授权参数。请回到飞书重新获取授权链接。",
                    ok=False,
                ),
                status_code=400,
            )
        bound = consume_oauth_state(state)
        if bound is None:
            return HTMLResponse(
                content=html_oauth_page(
                    title="链接已失效",
                    message="授权链接无效或已过期。请回到飞书重新说一次「我的任务」。",
                    ok=False,
                ),
                status_code=400,
            )
        config_id, open_id = bound
        row = channel_store.get_by_id(config_id)
        if row is None or row.channel != "feishu":
            return HTMLResponse(
                content=html_oauth_page(
                    title="配置异常",
                    message="应用配置不存在，请联系管理员检查飞书渠道配置。",
                    ok=False,
                ),
                status_code=400,
            )
        if not row.app_id.strip() or not row.app_secret.strip():
            return HTMLResponse(
                content=html_oauth_page(
                    title="配置异常",
                    message="飞书 App 凭证不完整，请管理员补全 App ID / Secret。",
                    ok=False,
                ),
                status_code=400,
            )
        try:
            redirect_uri = oauth_redirect_uri(
                _public_base_url(request, settings)
            )
            token = exchange_code_for_token(
                app_id=row.app_id,
                app_secret=row.app_secret,
                code=code,
                redirect_uri=redirect_uri,
            )
            channel_store.upsert_feishu_user_token(
                config_id,
                open_id,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_at=token.expires_at,
                refresh_expires_at=token.refresh_expires_at,
            )
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).exception(
                "feishu oauth exchange failed config=%s", config_id
            )
            return HTMLResponse(
                content=html_oauth_page(
                    title="授权失败",
                    message=f"换取访问凭证失败，请稍后重试。详情：{exc}",
                    ok=False,
                ),
                status_code=502,
            )
        return HTMLResponse(
            content=html_oauth_page(
                title="授权成功",
                message="飞书账号已授权。请回到飞书，再次发送「我有哪些未完成的任务」即可查询。",
                ok=True,
            )
        )

    @app.post("/webhooks/feishu")
    async def feishu_webhook(
        request: Request,
        background_tasks: BackgroundTasks,
        x_lark_request_timestamp: str | None = Header(default=None),
        x_lark_request_nonce: str | None = Header(default=None),
        x_lark_signature: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """飞书事件回调（公开固定地址）：按 App ID / Token 匹配页面配置。"""
        return await _dispatch_feishu_webhook(
            request,
            background_tasks,
            x_lark_request_timestamp=x_lark_request_timestamp,
            x_lark_request_nonce=x_lark_request_nonce,
            x_lark_signature=x_lark_signature,
        )

    @app.post("/webhooks/feishu/{config_id}")
    async def feishu_webhook_by_id(
        config_id: str,
        request: Request,
        background_tasks: BackgroundTasks,
        x_lark_request_timestamp: str | None = Header(default=None),
        x_lark_request_nonce: str | None = Header(default=None),
        x_lark_signature: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """兼容旧 URL（带 config_id）；新配置请用 /webhooks/feishu。"""
        row = channel_store.get_by_id(config_id)
        if row is None or row.channel != "feishu":
            raise HTTPException(status_code=404, detail="未知的飞书渠道配置")
        return await _dispatch_feishu_webhook(
            request,
            background_tasks,
            fixed_row=row,
            x_lark_request_timestamp=x_lark_request_timestamp,
            x_lark_request_nonce=x_lark_request_nonce,
            x_lark_signature=x_lark_signature,
        )

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
            bot.channel_ctx = ChannelContext(
                channel="web",
                public_base=_public_base_url(request, settings),
            )
            try:
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
            finally:
                bot.channel_ctx = None

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
        _: dict[str, Any] = Depends(require_auth),
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

    @app.get("/channels/feishu", response_model=ApiResponse[FeishuChannelData])
    def channels_feishu_get(
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        owner = str(user["sub"])
        row = channel_store.get_for_owner(owner, "feishu")
        if row is None:
            return ok(_empty_feishu(owner))
        return ok(_feishu_public(row))

    @app.get(
        "/integrations/dify/keys",
        response_model=ApiResponse[DifyApiKeyListData],
    )
    def integrations_dify_keys_list(
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        items = [
            DifyApiKeyItem(**row.to_public_dict()).model_dump()
            for row in channel_store.list_dify_api_keys()
        ]
        return ok({"items": items, "retrieval_path": "/retrieval"})

    @app.post(
        "/integrations/dify/keys",
        response_model=ApiResponse[DifyApiKeyItem],
    )
    def integrations_dify_keys_create(
        req: DifyApiKeyCreateRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            with channel_lock:
                plaintext, row = channel_store.create_dify_api_key(
                    name=req.name,
                    endpoint=req.endpoint,
                    knowledge_id=req.knowledge_id,
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ok(
            row.to_public_dict(api_key_plaintext=plaintext),
            message="已生成 API Key，请立即复制",
        )

    @app.post(
        "/integrations/dify/keys/{key_id}/update",
        response_model=ApiResponse[DifyApiKeyItem],
    )
    def integrations_dify_keys_update(
        key_id: str,
        req: DifyApiKeyUpdateRequest,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        try:
            with channel_lock:
                row = channel_store.update_dify_api_key(
                    key_id,
                    name=req.name,
                    endpoint=req.endpoint,
                    knowledge_id=req.knowledge_id,
                )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ok(row.to_public_dict(), message="已更新")

    @app.post(
        "/integrations/dify/keys/{key_id}/delete",
        response_model=ApiResponse[bool],
    )
    def integrations_dify_keys_delete(
        key_id: str,
        _: dict[str, Any] = Depends(require_ops),
    ) -> dict[str, Any]:
        with channel_lock:
            deleted = channel_store.delete_dify_api_key(key_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="配置不存在")
        return ok(True, message="已删除")

    @app.post(
        "/channels/feishu/update",
        response_model=ApiResponse[FeishuChannelData],
    )
    def channels_feishu_update(
        req: FeishuChannelUpdateRequest,
        user: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        owner = str(user["sub"])

        def _optional_secret(value: str | None) -> str | None:
            # None / 纯空白 → 保留原值；有内容 → 覆盖
            if value is None:
                return None
            if not value.strip():
                return None
            return value.strip()

        existing = channel_store.get_for_owner(owner, "feishu")
        merged_secret = _optional_secret(req.app_secret)
        if merged_secret is None and existing is not None:
            merged_secret = existing.app_secret
        merged_secret = (merged_secret or "").strip()
        if req.enabled and req.app_id.strip() and merged_secret:
            try:
                verify_app_credentials(req.app_id.strip(), merged_secret)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        try:
            with channel_lock:
                row = channel_store.upsert_feishu(
                    owner,
                    enabled=req.enabled,
                    app_id=req.app_id,
                    app_secret=_optional_secret(req.app_secret),
                    verification_token=_optional_secret(req.verification_token),
                    encrypt_key=_optional_secret(req.encrypt_key),
                )
        except AppIdTakenError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ok(_feishu_public(row), message="飞书渠道配置已保存")

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
