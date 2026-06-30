"""ATDD — communities Phase B2 migration (#32).

0010 adds ``communities.default_channel_id`` (a plain non-FK String) and backfills
the seeded "Aiko" community's value to its lowest-id channel. These specs pin that
against a realistic 0009 DB (the live shape before B2 ships), driving the real
alembic runner.
"""
from __future__ import annotations

from alembic import command
from sqlalchemy import create_engine, text

from aiko_gateway import migrate
from aiko_gateway.config import settings
from aiko_gateway.domain.models import DEFAULT_COMMUNITY_ID

_TS = "2026-01-01T00:00:00+00:00"


def _at_0009_with(tmp_path, monkeypatch, *, channels):
    """A throwaway DB upgraded to 0009 (B1 done) with the given channel ids. 0009
    seeds the Aiko community and assigns every channel to it, so after this the
    channels already carry community_id=DEFAULT_COMMUNITY_ID."""
    db = tmp_path / "b2.db"
    monkeypatch.setattr(settings, "db_url", f"sqlite+aiosqlite:///{db}")
    cfg = migrate._alembic_config()
    command.upgrade(cfg, "0008")
    sync_url = f"sqlite:///{db}"
    engine = create_engine(sync_url)
    try:
        with engine.begin() as c:
            for ch in channels:
                c.execute(text(
                    "INSERT INTO channels (id, name, kind, aiko_channel, is_private, "
                    "join_policy, created_at) VALUES "
                    "(:id, :id, 'standard', :ak, 0, 'invite_only', :ts)"),
                    {"id": ch, "ak": f"aiko/{ch}", "ts": _TS})
    finally:
        engine.dispose()
    command.upgrade(cfg, "0009")  # seed Aiko + assign channels
    return cfg, sync_url


def test_0010_backfills_default_channel_to_lowest_id(tmp_path, monkeypatch):
    # Insert OUT of id order to prove the backfill picks the lowest, not the last.
    cfg, sync_url = _at_0009_with(
        tmp_path, monkeypatch, channels=["c3", "c1", "c2"])
    command.upgrade(cfg, "head")  # -> 0010

    engine = create_engine(sync_url)
    try:
        with engine.connect() as c:
            default_ch = c.execute(text(
                "SELECT default_channel_id FROM communities WHERE id=:id"),
                {"id": DEFAULT_COMMUNITY_ID}).scalar()
            assert default_ch == "c1"  # lowest id among the Aiko channels
    finally:
        engine.dispose()


def test_0010_on_community_without_channels_leaves_null(tmp_path, monkeypatch):
    """A community with no channels (empty prod / fresh DB) keeps a NULL default —
    the backfill must not invent a channel id."""
    cfg, sync_url = _at_0009_with(tmp_path, monkeypatch, channels=[])
    command.upgrade(cfg, "head")

    engine = create_engine(sync_url)
    try:
        with engine.connect() as c:
            default_ch = c.execute(text(
                "SELECT default_channel_id FROM communities WHERE id=:id"),
                {"id": DEFAULT_COMMUNITY_ID}).scalar()
            assert default_ch is None
    finally:
        engine.dispose()


def test_0010_downgrade_drops_the_column(tmp_path, monkeypatch):
    cfg, sync_url = _at_0009_with(tmp_path, monkeypatch, channels=["c1"])
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0009")

    engine = create_engine(sync_url)
    try:
        with engine.connect() as c:
            cols = {r[1] for r in c.execute(
                text("PRAGMA table_info(communities)")).all()}
            assert "default_channel_id" not in cols
    finally:
        engine.dispose()
