"""Async SQLAlchemy engine + session factory.

Dev uses a docker Postgres on :5433 (see spike/devstack notes); deploy uses the
imagineering Postgres via DB_URL from SOPS. Phase 1 creates tables with
``create_all``; alembic migrations (one revision per phase) land before deploy
(tracked task).
"""
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from .config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.db_url, echo=False, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_models() -> None:
    # Import models so they register on Base.metadata before create_all.
    from .domain import models  # noqa: F401
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
