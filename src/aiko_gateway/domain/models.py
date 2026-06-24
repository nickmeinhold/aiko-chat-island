"""ORM models — Phase 1 subset (plan §A4).

Hand-written SQLAlchemy 2.0 ORM (no codegen). This is the persistence half of
the trust boundary: `messages.sender_user_id` is set server-side from the
authenticated user (invariant I5), never from client input. Reactions, media,
read_positions, devices, message_edits arrive in later phases (each its own
alembic revision).
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base
from .ids import new_ulid


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    # Wire-attribution identity on the aiko bus (defaults to username).
    aiko_username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Channel(Base):
    __tablename__ = "channels"
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 'standard' | 'llm' | 'robot' | 'dm' — maps to an aiko recipient string.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="standard")
    aiko_channel: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Self-join policy for PRIVATE channels (#46). 'invite_only' (default) = a
    # user may only be added by a channel admin; 'open' = any authenticated user
    # may self-join via /join. Public channels (is_private=False) ignore this —
    # they are open to everyone and need no membership at all. Default is the
    # safe one: a private channel is invite_only until explicitly opened, so a
    # channel created without thinking about policy can never be self-joined.
    join_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="invite_only")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Membership(Base):
    __tablename__ = "memberships"
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")  # member|admin
    can_post: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    joined_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Idempotent optimistic send: a resent client_msg_id no-ops, not dupes.
        UniqueConstraint("channel_id", "client_msg_id", name="uq_channel_client_msg"),
    )
    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=new_ulid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"), nullable=False, index=True)
    # Null when the sender is a non-gateway aiko actor (llm/robot/external REPL).
    sender_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    sender_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # human|llm|robot|actor
    sender_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    reply_to: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    client_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # True if it arrived FROM the bus; False if it originated gateway-side.
    aiko_origin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    edited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
