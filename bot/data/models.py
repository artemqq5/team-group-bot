from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=True)
    group_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    time: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    first_name: Mapped[str] = mapped_column(Text, nullable=True)
    language_code: Mapped[str] = mapped_column(String(100), nullable=True)
    title_group: Mapped[str] = mapped_column("title_gruop", Text, nullable=True)
    link_group: Mapped[str] = mapped_column(Text, nullable=True)


class ChatModel(Base):
    __tablename__ = "chats"

    group_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=True)
    creo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    shop_google: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    shop_fb: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    console: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    apps: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    affiliate_mp: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    partner_mp: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    media_mt: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    agency_google: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    agency_fb: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    media_mp: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    partner_mt: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    time: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    link: Mapped[str] = mapped_column(Text, nullable=True)


class AdminModel(Base):
    __tablename__ = "admins"

    telegram_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(255), nullable=True, default="ADMIN", server_default="ADMIN")


class BroadcastModel(Base):
    __tablename__ = "broadcasts"
    __table_args__ = (Index("ix_broadcasts_sent_at", "sent_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    admin_id: Mapped[str] = mapped_column(String(255), nullable=False)
    admin_name: Mapped[str] = mapped_column(String(255), nullable=True)
    category: Mapped[str] = mapped_column(String(255), nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=True)
    has_photo: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    has_buttons: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    sent_at: Mapped[DateTime] = mapped_column(DateTime, nullable=False)
    groups_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ChatMessageModel(Base):
    __tablename__ = "chat_messages"
    __table_args__ = (
        UniqueConstraint("group_id", "message_id", name="uq_chat_messages_group_msg"),
        Index("ix_chat_messages_group_sent", "group_id", "sent_at"),
        Index("ix_chat_messages_sent", "sent_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    reply_to_message_id: Mapped[int] = mapped_column(BigInteger, nullable=True)
    author_id: Mapped[str] = mapped_column(String(255), nullable=True)
    author_username: Mapped[str] = mapped_column(String(255), nullable=True)
    author_first_name: Mapped[str] = mapped_column(String(255), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=True)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    has_media: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    sent_at: Mapped[DateTime] = mapped_column(DateTime, nullable=False)
    edit_date: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    is_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class McpKeyModel(Base):
    __tablename__ = "mcp_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime, nullable=False)
    categories: Mapped[list] = mapped_column(JSON, nullable=False)
    expires_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    revoked_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)
    revoked_by: Mapped[str] = mapped_column(String(255), nullable=True)
    last_used_at: Mapped[DateTime] = mapped_column(DateTime, nullable=True)


class McpAuditLogModel(Base):
    __tablename__ = "mcp_audit_log"
    __table_args__ = (
        Index("ix_mcp_audit_key_created", "key_id", "created_at"),
        Index("ix_mcp_audit_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime, nullable=False)
    key_id: Mapped[int] = mapped_column(Integer, nullable=True)  # без FK: журнал має пережити будь-що
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=True)
    arguments: Mapped[dict] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | denied | error
    # no_token, invalid_token, revoked, expired, out_of_scope, bad_args, rate_limited, internal
    reason: Mapped[str] = mapped_column(String(64), nullable=True)
    result_count: Mapped[int] = mapped_column(Integer, nullable=True)
    client_ip: Mapped[str] = mapped_column(String(64), nullable=True)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
