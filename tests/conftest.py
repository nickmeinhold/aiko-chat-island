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

# Give the test harness a >= 32-byte JWT secret. The dev default
# ("dev-insecure-change-me", 22 bytes) is below PyJWT's HS256 floor, so every
# token issued/verified in the suite triggers an `InsecureKeyLengthWarning`.
# That warning is dev/test-only noise: production already fail-closed-rejects a
# sub-32-byte secret in config.py's `_harden_for_production`, so PyJWT never
# sees a short key there. We silence the warning at its SOURCE (a compliant key)
# rather than filtering it — keeping PyJWT's machinery armed to flag a genuinely
# short key if one ever leaks into a test. setdefault so a CI-supplied secret
# still wins, mirroring the ENVIRONMENT handling above.
os.environ.setdefault("JWT_SECRET", "test-secret-at-least-32-bytes-long!!")

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


import pytest  # noqa: E402
from aiko_gateway.domain.rate_limit import limiter as _rate_limiter  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The rate limiter (#28) is a module-global counter shared across the whole
    process. Reset it around every test so one test's requests can't consume
    another's per-IP budget (all tests share the same client key) — and so a
    rate-limit test starts from a clean window."""
    _rate_limiter.reset()
    yield
    _rate_limiter.reset()
