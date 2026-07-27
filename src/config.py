"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    dashscope_api_key: str
    openai_api_base: str
    chat_model: str
    embedding_model: str
    intent_llm: bool
    intent_model: str
    answer_temperature: float
    qdrant_path: Path
    collection_name: str
    faq_dir: Path
    docs_dir: Path
    assets_dir: Path
    asset_base_url: str
    jwt_secret: str
    jwt_access_ttl: int
    jwt_refresh_ttl: int
    admin_username: str
    admin_password: str
    auth_allow_register: bool
    auth_db_path: Path
    session_db_path: Path
    business_db_path: Path
    chat_log_db_path: Path
    faq_db_path: Path
    sensitive_db_path: Path
    top_k: int
    clarify_threshold: float
    direct_threshold: float
    clarify_count: int
    faq_max_similar: int
    chunk_size: int
    chunk_overlap: int
    hybrid_search: bool
    hybrid_vector_k: int
    hybrid_bm25_k: int
    rerank_enabled: bool
    rerank_model: str
    rerank_top_n: int
    # 噪声底线：低于此分一律丢（与「高相关口语改写 ~0.2」区分）
    rerank_min_score: float
    # 近原句高相关带：高于此分可按硬命中放行
    rerank_high_score: float
    # 相对阈值：score >= top * relative 才进入候选（适配精排分标定漂移）
    rerank_relative: float
    # 企业微信渠道（可选；未启用不影响 /chat）
    wecom_enabled: bool
    wecom_corp_id: str
    wecom_agent_id: str
    wecom_secret: str
    wecom_token: str
    wecom_aes_key: str
    wecom_session_prefix: str
    wecom_unsupported_msg_reply: str

    @property
    def score_threshold(self) -> float:
        """Backward-compatible alias for clarify_threshold."""
        return self.clarify_threshold

    @classmethod
    def from_env(cls) -> Settings:
        dashscope_api_key = (
            os.getenv("DASHSCOPE_API_KEY") or os.getenv("OPENAI_API_KEY", "")
        )
        if not dashscope_api_key or dashscope_api_key.startswith("sk-your"):
            raise ValueError(
                "未配置有效的 DASHSCOPE_API_KEY。"
                "请复制 .env.example 为 .env 并填入通义 API Key。"
            )

        qdrant_path = Path(os.getenv("QDRANT_PATH", "./data/qdrant_db"))
        if not qdrant_path.is_absolute():
            qdrant_path = PROJECT_ROOT / qdrant_path

        faq_dir = PROJECT_ROOT / "data" / "faq"
        docs_dir = PROJECT_ROOT / "data" / "docs"
        assets_dir = PROJECT_ROOT / "data" / "assets"
        # 空则 /chat 用请求 Host 拼绝对 URL；也可固定写成 http://127.0.0.1:8000
        asset_base_url = os.getenv("ASSET_BASE_URL", "").rstrip("/")
        # JWT 鉴权：登录换 Bearer access_token；公开端点仅 /health、企微回调
        jwt_secret = os.getenv("JWT_SECRET", "").strip()
        if not jwt_secret or len(jwt_secret) < 32:
            raise ValueError(
                "未配置有效的 JWT_SECRET（至少 32 字符）。"
                "请复制 .env.example 为 .env 并填入。"
            )
        admin_username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
        admin_password = os.getenv("ADMIN_PASSWORD", "").strip()
        if not admin_password:
            raise ValueError(
                "未配置 ADMIN_PASSWORD。请在 .env 中设置管理后台登录密码。"
            )
        jwt_access_ttl = int(os.getenv("JWT_ACCESS_TTL", "7200"))
        jwt_refresh_ttl = int(os.getenv("JWT_REFRESH_TTL", "604800"))
        auth_allow_register = os.getenv(
            "AUTH_ALLOW_REGISTER", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}

        auth_db_path = Path(os.getenv("AUTH_DB_PATH", "./data/auth.db"))
        if not auth_db_path.is_absolute():
            auth_db_path = PROJECT_ROOT / auth_db_path

        session_db_path = Path(
            os.getenv("SESSION_DB_PATH", "./data/sessions.db")
        )
        if not session_db_path.is_absolute():
            session_db_path = PROJECT_ROOT / session_db_path

        business_db_path = Path(
            os.getenv("BUSINESS_DB_PATH", "./data/business.db")
        )
        if not business_db_path.is_absolute():
            business_db_path = PROJECT_ROOT / business_db_path

        chat_log_db_path = Path(
            os.getenv("CHAT_LOG_DB_PATH", "./data/chat_logs.db")
        )
        if not chat_log_db_path.is_absolute():
            chat_log_db_path = PROJECT_ROOT / chat_log_db_path

        faq_db_path = Path(os.getenv("FAQ_DB_PATH", "./data/faqs.db"))
        if not faq_db_path.is_absolute():
            faq_db_path = PROJECT_ROOT / faq_db_path

        sensitive_db_path = Path(
            os.getenv("SENSITIVE_DB_PATH", "./data/sensitive.db")
        )
        if not sensitive_db_path.is_absolute():
            sensitive_db_path = PROJECT_ROOT / sensitive_db_path

        # Prefer CLARIFY_THRESHOLD; fall back to legacy SCORE_THRESHOLD.
        clarify = float(
            os.getenv("CLARIFY_THRESHOLD")
            or os.getenv("SCORE_THRESHOLD", "0.45")
        )
        direct = float(os.getenv("DIRECT_THRESHOLD", "0.72"))
        if direct < clarify:
            direct = clarify

        return cls(
            dashscope_api_key=dashscope_api_key,
            openai_api_base=os.getenv(
                "OPENAI_API_BASE",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            chat_model=os.getenv("CHAT_MODEL", "qwen-plus"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "text-embedding-v4"),
            intent_llm=os.getenv("INTENT_LLM", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            intent_model=(
                os.getenv("INTENT_MODEL", "").strip()
                or os.getenv("CHAT_MODEL", "qwen-plus")
            ),
            answer_temperature=float(os.getenv("ANSWER_TEMPERATURE", "0")),
            qdrant_path=qdrant_path,
            collection_name=os.getenv("COLLECTION_NAME", "customer_service_kb"),
            faq_dir=faq_dir,
            docs_dir=docs_dir,
            assets_dir=assets_dir,
            asset_base_url=asset_base_url,
            jwt_secret=jwt_secret,
            jwt_access_ttl=jwt_access_ttl,
            jwt_refresh_ttl=jwt_refresh_ttl,
            admin_username=admin_username,
            admin_password=admin_password,
            auth_allow_register=auth_allow_register,
            auth_db_path=auth_db_path,
            session_db_path=session_db_path,
            business_db_path=business_db_path,
            chat_log_db_path=chat_log_db_path,
            faq_db_path=faq_db_path,
            sensitive_db_path=sensitive_db_path,
            top_k=int(os.getenv("TOP_K", "4")),
            clarify_threshold=clarify,
            direct_threshold=direct,
            clarify_count=int(os.getenv("CLARIFY_COUNT", "3")),
            faq_max_similar=int(os.getenv("FAQ_MAX_SIMILAR", "30")),
            chunk_size=int(os.getenv("CHUNK_SIZE", "500")),
            chunk_overlap=int(os.getenv("CHUNK_OVERLAP", "50")),
            hybrid_search=os.getenv("HYBRID_SEARCH", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            hybrid_vector_k=int(os.getenv("HYBRID_VECTOR_K", "20")),
            hybrid_bm25_k=int(os.getenv("HYBRID_BM25_K", "20")),
            rerank_enabled=os.getenv("RERANK_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            rerank_model=os.getenv("RERANK_MODEL", "gte-rerank-v2").strip()
            or "gte-rerank-v2",
            rerank_top_n=int(os.getenv("RERANK_TOP_N", "8")),
            rerank_min_score=float(os.getenv("RERANK_MIN_SCORE", "0.05")),
            rerank_high_score=float(os.getenv("RERANK_HIGH_SCORE", "0.50")),
            rerank_relative=float(os.getenv("RERANK_RELATIVE", "0.65")),
            wecom_enabled=os.getenv("WECOM_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            wecom_corp_id=os.getenv("WECOM_CORP_ID", "").strip(),
            wecom_agent_id=os.getenv("WECOM_AGENT_ID", "").strip(),
            wecom_secret=os.getenv("WECOM_SECRET", "").strip(),
            wecom_token=os.getenv("WECOM_TOKEN", "").strip(),
            wecom_aes_key=os.getenv("WECOM_AES_KEY", "").strip(),
            wecom_session_prefix=os.getenv("WECOM_SESSION_PREFIX", "ww:").strip()
            or "ww:",
            wecom_unsupported_msg_reply=os.getenv(
                "WECOM_UNSUPPORTED_MSG_REPLY",
                "当前仅支持文字消息，请直接输入问题。",
            ).strip()
            or "当前仅支持文字消息，请直接输入问题。",
        )


def get_settings() -> Settings:
    return Settings.from_env()
