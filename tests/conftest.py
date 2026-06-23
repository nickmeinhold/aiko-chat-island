"""Shared test fixtures.

Phase-1 tests run against an in-memory async SQLite engine — fast, isolated, and
Postgres-free. The production engine (db.py) targets Postgres; tests build their
own engine + session so they never touch a real DB. `Base.metadata` is shared, so
`create_all` here builds the same schema the app uses.
"""
from __future__ import annotations

import os

# Declare the test environment BEFORE any aiko_gateway import. The module-level
# `settings = Settings()` (reached via aiko_gateway.db below) now defaults to
# "production" and fail-closed-raises on the dev jwt_secret; the test harness is
# an explicitly-declared non-prod environment. setdefault so a real ENVIRONMENT
# (e.g. set by a future CI matrix) still wins.
os.environ.setdefault("ENVIRONMENT", "test")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession, async_sessionmaker, create_async_engine,
)

from aiko_gateway.db import Base  # noqa: E402
from aiko_gateway.domain import models  # noqa: E402,F401 — register tables on Base.metadata


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    """A fresh in-memory DB per test (schema created, torn down after)."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()
