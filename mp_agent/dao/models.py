from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# SQLite only autoincrements INTEGER PRIMARY KEY; keep BIGINT on other dialects.
_AutoIncPK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    pass


class GlobalProduct(Base):
    __tablename__ = "global_product"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_title: Mapped[str] = mapped_column(String(512), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (Index("idx_brand", "brand"),)


class PlatformProduct(Base):
    __tablename__ = "platform_product"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    keyword: Mapped[str] = mapped_column(String(256), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_original: Mapped[str | None] = mapped_column(String(64))
    currency: Mapped[str | None] = mapped_column(String(8))
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    review_count: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(String(1024))
    is_valid: Mapped[int] = mapped_column(Integer, default=1)
    global_product_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("global_product.id", ondelete="SET NULL")
    )
    match_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    crawl_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    detail: Mapped["PlatformProductDetail | None"] = relationship(
        back_populates="product", uselist=False, lazy="raise"
    )
    snapshots: Mapped[list["PlatformProductSnapshot"]] = relationship(
        back_populates="product", lazy="raise"
    )
    analysis_results: Mapped[list["AnalysisResult"]] = relationship(
        back_populates="product", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("platform", "platform_product_id", name="uq_platform_product"),
        Index("idx_keyword", "keyword"),
        Index("idx_platform", "platform"),
        Index("idx_crawl_time", "crawl_time"),
        Index("idx_global_product", "global_product_id"),
    )


class PlatformProductDetail(Base):
    __tablename__ = "platform_product_detail"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("platform_product.id", ondelete="CASCADE"), nullable=False
    )
    extra: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped["PlatformProduct"] = relationship(
        back_populates="detail", lazy="raise"
    )

    __table_args__ = (UniqueConstraint("product_id", name="uq_product"),)


class PlatformProductSnapshot(Base):
    __tablename__ = "platform_product_snapshot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("platform_product.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_product_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str | None] = mapped_column(String(512))
    price_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    price_original: Mapped[str | None] = mapped_column(String(64))
    rating: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    review_count: Mapped[int | None] = mapped_column(Integer)
    extra: Mapped[dict | None] = mapped_column(JSON)
    crawl_task_id: Mapped[int | None] = mapped_column(Integer)
    snapshotted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped["PlatformProduct"] = relationship(
        back_populates="snapshots", lazy="raise"
    )

    __table_args__ = (
        Index("idx_product_id", "product_id"),
        Index("idx_snapshotted_at", "snapshotted_at"),
    )


class CrawlTask(Base):
    __tablename__ = "crawl_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    keyword: Mapped[str] = mapped_column(String(256), nullable=False)
    target_count: Mapped[int] = mapped_column(SmallInteger, default=5)
    status: Mapped[str] = mapped_column(
        Enum("pending", "running", "done", "failed"), default="pending"
    )
    products_found: Mapped[int] = mapped_column(SmallInteger, default=0)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("idx_status", "status"),
        Index("idx_platform_kw", "platform", "keyword"),
    )


class AnalysisResult(Base):
    __tablename__ = "analysis_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("platform_product.id", ondelete="CASCADE"), nullable=False
    )
    crawl_task_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("crawl_task.id", ondelete="SET NULL")
    )
    core_selling_points: Mapped[str | None] = mapped_column(Text)
    pros: Mapped[list | None] = mapped_column(JSON)
    cons: Mapped[list | None] = mapped_column(JSON)
    overall: Mapped[str | None] = mapped_column(Text)
    positioning: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(256))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    product: Mapped["PlatformProduct"] = relationship(
        back_populates="analysis_results", lazy="raise"
    )

    __table_args__ = (
        Index("idx_product_id_ar", "product_id"),
        Index("idx_crawl_task_id", "crawl_task_id"),
    )


class Review(Base):
    __tablename__ = "review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("platform_product.id", ondelete="CASCADE"), nullable=False
    )
    platform_review_id: Mapped[str | None] = mapped_column(String(128))
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    title: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(String(256))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)
    country: Mapped[str | None] = mapped_column(String(64))
    helpful_count: Mapped[int] = mapped_column(Integer, default=0)
    sentiment: Mapped[str | None] = mapped_column(
        Enum("positive", "negative", "neutral")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("product_id", "platform_review_id", name="uq_review"),
        Index("idx_product_id_rv", "product_id"),
        Index("idx_rating_rv", "rating"),
    )


# ---------------------------------------------------------------------------
# ReplyKit core stores (migrated from src/* sqlite3)
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(32), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    refresh_tokens: Mapped[list["RefreshToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )
    chat_sessions: Mapped[list["ChatSession"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (Index("idx_users_role_enabled", "role", "enabled"),)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.username", ondelete="CASCADE"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="refresh_tokens")

    __table_args__ = (
        Index("idx_rt_username", "username"),
        Index("idx_rt_expires", "expires_at"),
    )


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.username", ondelete="CASCADE"), nullable=True
    )
    title: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    history_json: Mapped[list[dict] | list] = mapped_column(JSON, default=list, nullable=False)
    bot_state_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user: Mapped["User | None"] = relationship(back_populates="chat_sessions")
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
        lazy="raise",
    )

    __table_args__ = (Index("idx_chat_session_user_updated", "username", "updated_at"),)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(_AutoIncPK, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    route: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    is_handoff: Mapped[bool] = mapped_column(default=False, nullable=False)
    sources: Mapped[list[str] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")

    __table_args__ = (
        Index("idx_chat_message_session", "session_id"),
        Index("idx_chat_message_created", "created_at"),
    )


class ChatLog(Base):
    """Append-only Q&A turn log (one row per user-question → answer)."""

    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(_AutoIncPK, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    username: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[float | None] = mapped_column(Numeric(6, 4))
    sources: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    is_handoff: Mapped[bool] = mapped_column(default=False, nullable=False)
    route: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), default="", nullable=False)

    __table_args__ = (
        Index("idx_chat_logs_username", "username"),
        Index("idx_chat_logs_route", "route"),
    )


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    icon: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    category: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    runtime: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("idx_agents_enabled_sort", "enabled", "sort_order"),
    )


class Faq(Base):
    __tablename__ = "faqs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    similar: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    # Document ACL: public = all authenticated users; private = owner (+ ops)
    owner_username: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), default="public", nullable=False)
    # False: may be retrieved privately but must not be sent to public model APIs
    allow_egress: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("idx_faqs_updated", "updated_at"),
        Index("idx_faqs_category", "category"),
        Index("idx_faqs_enabled", "enabled"),
        Index("idx_faqs_owner", "owner_username"),
        Index("idx_faqs_visibility", "visibility"),
    )


class SensitiveWord(Base):
    __tablename__ = "sensitive_words"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pattern: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(default=True, nullable=False)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("idx_sensitive_updated", "updated_at"),
        Index("idx_sensitive_enabled", "enabled"),
    )


class ChannelConfig(Base):
    __tablename__ = "channel_configs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_username: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    app_id: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    app_secret: Mapped[str] = mapped_column(Text, default="", nullable=False)
    verification_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    encrypt_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    user_tokens: Mapped[list["FeishuUserToken"]] = relationship(
        back_populates="config", cascade="all, delete-orphan", lazy="raise"
    )

    __table_args__ = (
        UniqueConstraint("owner_username", "channel", name="uq_channel_owner"),
        Index("idx_channel_owner", "owner_username"),
        Index("idx_channel_enabled", "channel", "enabled"),
        Index("idx_channel_app_id", "channel", "app_id"),
    )


class FeishuUserToken(Base):
    __tablename__ = "feishu_user_tokens"

    config_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("channel_configs.id", ondelete="CASCADE"), primary_key=True
    )
    open_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, default="", nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    refresh_expires_at: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    config: Mapped["ChannelConfig"] = relationship(back_populates="user_tokens")


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )


class DifyApiKey(Base):
    __tablename__ = "dify_api_keys"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    knowledge_id: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


class Order(Base):
    __tablename__ = "orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(128), nullable=False)
    carrier: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    tracking_no: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    eta: Mapped[str] = mapped_column(String(256), default="", nullable=False)
    last_event: Mapped[str] = mapped_column(Text, default="", nullable=False)


class Ticket(Base):
    __tablename__ = "tickets"

    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    order_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64), default="open", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
