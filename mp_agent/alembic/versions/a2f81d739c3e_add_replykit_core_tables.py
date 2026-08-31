"""add replykit core stores tables

Revision ID: a2f81d739c3e
Revises: 314c543ff8ae
Create Date: 2026-05-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a2f81d739c3e'
down_revision: Union[str, Sequence[str], None] = '314c543ff8ae'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("password_hash", sa.String(128), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="user"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("username"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_users_role_enabled", "users", ["role", "enabled"])

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token", sa.String(128), nullable=False),
        sa.Column("username", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["username"], ["users.username"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_rt_username", "refresh_tokens", ["username"])
    op.create_index("idx_rt_expires", "refresh_tokens", ["expires_at"])

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("username", sa.String(32), nullable=True),
        sa.Column("title", sa.String(120), nullable=False, server_default=""),
        sa.Column("history_json", sa.JSON(), nullable=False),
        sa.Column("bot_state_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["username"], ["users.username"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_chat_session_user_updated", "chat_sessions", ["username", "updated_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("route", sa.String(32), nullable=False, server_default=""),
        sa.Column("strategy", sa.String(32), nullable=False, server_default=""),
        sa.Column("score", sa.Numeric(6, 4), nullable=True),
        sa.Column("is_handoff", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["chat_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_chat_message_session", "chat_messages", ["session_id"])
    op.create_index("idx_chat_message_created", "chat_messages", ["created_at"])

    op.create_table(
        "chat_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("username", sa.String(32), nullable=False, server_default=""),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(6, 4), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("is_handoff", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("route", sa.String(32), nullable=False, server_default=""),
        sa.Column("strategy", sa.String(32), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_chat_logs_created_at", "chat_logs", ["created_at"])
    op.create_index("idx_chat_logs_username", "chat_logs", ["username"])
    op.create_index("idx_chat_logs_route", "chat_logs", ["route"])

    op.create_table(
        "agents",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("icon", sa.String(64), nullable=False, server_default=""),
        sa.Column("category", sa.String(128), nullable=False, server_default=""),
        sa.Column("runtime", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_agents_enabled_sort", "agents", ["enabled", "sort_order"])

    op.create_table(
        "faqs",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("category", sa.String(128), nullable=False, server_default=""),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("similar", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_faqs_updated", "faqs", ["updated_at"])
    op.create_index("idx_faqs_category", "faqs", ["category"])
    op.create_index("idx_faqs_enabled", "faqs", ["enabled"])

    op.create_table(
        "sensitive_words",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("pattern", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pattern"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_sensitive_updated", "sensitive_words", ["updated_at"])
    op.create_index("idx_sensitive_enabled", "sensitive_words", ["enabled"])

    op.create_table(
        "channel_configs",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("owner_username", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("app_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("app_secret", sa.Text(), nullable=False, server_default=""),
        sa.Column("verification_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("encrypt_key", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_username", "channel", name="uq_channel_owner"),
        mysql_charset="utf8mb4",
    )
    op.create_index("idx_channel_owner", "channel_configs", ["owner_username"])
    op.create_index("idx_channel_enabled", "channel_configs", ["channel", "enabled"])
    op.create_index("idx_channel_app_id", "channel_configs", ["channel", "app_id"])

    op.create_table(
        "feishu_user_tokens",
        sa.Column("config_id", sa.String(64), nullable=False),
        sa.Column("open_id", sa.String(128), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("refresh_token", sa.Text(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("refresh_expires_at", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["config_id"], ["channel_configs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("config_id", "open_id"),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "dify_api_keys",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("knowledge_id", sa.String(128), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("api_key"),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "orders",
        sa.Column("order_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(128), nullable=False),
        sa.Column("carrier", sa.String(128), nullable=False, server_default=""),
        sa.Column("tracking_no", sa.String(128), nullable=False, server_default=""),
        sa.Column("eta", sa.String(256), nullable=False, server_default=""),
        sa.Column("last_event", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("order_id"),
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "tickets",
        sa.Column("ticket_id", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("order_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(64), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("ticket_id"),
        mysql_charset="utf8mb4",
    )


def downgrade() -> None:
    op.drop_table("tickets")
    op.drop_table("orders")
    op.drop_table("dify_api_keys")
    op.drop_table("platform_settings")
    op.drop_table("feishu_user_tokens")
    op.drop_index("idx_channel_app_id", table_name="channel_configs")
    op.drop_index("idx_channel_enabled", table_name="channel_configs")
    op.drop_index("idx_channel_owner", table_name="channel_configs")
    op.drop_table("channel_configs")
    op.drop_index("idx_sensitive_enabled", table_name="sensitive_words")
    op.drop_index("idx_sensitive_updated", table_name="sensitive_words")
    op.drop_table("sensitive_words")
    op.drop_index("idx_faqs_enabled", table_name="faqs")
    op.drop_index("idx_faqs_category", table_name="faqs")
    op.drop_index("idx_faqs_updated", table_name="faqs")
    op.drop_table("faqs")
    op.drop_index("idx_agents_enabled_sort", table_name="agents")
    op.drop_table("agents")
    op.drop_index("idx_chat_logs_route", table_name="chat_logs")
    op.drop_index("idx_chat_logs_username", table_name="chat_logs")
    op.drop_index("ix_chat_logs_created_at", table_name="chat_logs")
    op.drop_table("chat_logs")
    op.drop_index("idx_chat_message_created", table_name="chat_messages")
    op.drop_index("idx_chat_message_session", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("idx_chat_session_user_updated", table_name="chat_sessions")
    op.drop_table("chat_sessions")
    op.drop_index("idx_rt_expires", table_name="refresh_tokens")
    op.drop_index("idx_rt_username", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index("idx_users_role_enabled", table_name="users")
    op.drop_table("users")
