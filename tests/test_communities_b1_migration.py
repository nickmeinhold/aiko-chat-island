"""ATDD — communities Phase B1 migration (#32).

0009 must, on a pre-community DB, do three things and nothing user-visible:
seed ONE default community ("Aiko"), assign every existing channel to it, and
auto-join every existing user. These specs pin that data migration against a
realistic 0008 DB (the live shape before this ships), driving the real
alembic runner — not a reimplementation.
"""
from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text

from aiko_gateway import migrate
from aiko_gateway.config import settings
from aiko_gateway.domain.models import DEFAULT_COMMUNITY_ID

_TS = "2026-01-01T00:00:00+00:00"


def _at_0008_with(tmp_path, monkeypatch, *, users, channels):
    """A throwaway DB upgraded to 0008 (pre-communities) and seeded with the given
    user + channel ids via raw SQL (channels have no community_id at 0008)."""
    db = tmp_path / "b1.db"
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    cfg = migrate._alembic_config()
    command.upgrade(cfg, "0008")
    sync_url = f"sqlite:///{db}"
    engine = create_engine(sync_url)
    try:
        with engine.begin() as c:
            for u in users:
                c.execute(text(
                    "INSERT INTO users (id, username, display_name, aiko_username, "
                    "created_at) VALUES (:id, :id, :id, :id, :ts)"),
                    {"id": u, "ts": _TS})
            for ch in channels:
                c.execute(text(
                    "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
                    "join_policy, created_at) VALUES "
                    "(:id, :id, 'standard', :ak, 0, 'invite_only', :ts)"),
                    {"id": ch, "ak": f"aiko/{ch}", "ts": _TS})
    finally:
        engine.dispose()
    return cfg, sync_url


def test_0009_seeds_community_assigns_channels_and_autojoins_users(
        tmp_path, monkeypatch):
    cfg, sync_url = _at_0008_with(
        tmp_path, monkeypatch, users=["u1", "u2", "u3"], channels=["c1", "c2"])
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    try:
        with engine.connect() as c:
            # ONE default community, seeded with the right projection fields.
            assert c.execute(
                text("SELECT COUNT(*) FROM communities")).scalar() == 1
            row = c.execute(text(
                "SELECT name, visibility, category, member_count, owner_id "
                "FROM communities WHERE id=:id"),
                {"id": DEFAULT_COMMUNITY_ID}).one()
            assert row.name == "Aiko"
            assert row.visibility == "public"
            assert row.category == "general"
            assert row.member_count == 3      # seeded to the existing user count
            assert row.owner_id is None        # system-owned

            # EVERY existing channel assigned to the default community.
            total = c.execute(text("SELECT COUNT(*) FROM channels")).scalar()
            assigned = c.execute(text(
                "SELECT COUNT(*) FROM channels WHERE community_id=:id"),
                {"id": DEFAULT_COMMUNITY_ID}).scalar()
            assert total == 2 and assigned == 2

            # EVERY existing user auto-joined as a member.
            joined = c.execute(text(
                "SELECT user_id, role FROM community_memberships "
                "WHERE community_id=:id ORDER BY user_id"),
                {"id": DEFAULT_COMMUNITY_ID}).all()
            assert [r.user_id for r in joined] == ["u1", "u2", "u3"]
            assert all(r.role == "member" for r in joined)
    finally:
        engine.dispose()


def test_0009_on_empty_db_seeds_community_with_zero_members(tmp_path, monkeypatch):
    """No users/channels: the default community is still seeded (member_count 0,
    no memberships) — so a fresh prod DB also gets the hierarchy foundation."""
    cfg, sync_url = _at_0008_with(tmp_path, monkeypatch, users=[], channels=[])
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    try:
        with engine.connect() as c:
            mc = c.execute(text(
                "SELECT member_count FROM communities WHERE id=:id"),
                {"id": DEFAULT_COMMUNITY_ID}).scalar()
            assert mc == 0
            assert c.execute(
                text("SELECT COUNT(*) FROM community_memberships")).scalar() == 0
    finally:
        engine.dispose()
