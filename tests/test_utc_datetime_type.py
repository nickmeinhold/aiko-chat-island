"""The naive/aware datetime class, closed at the column boundary (claude-tasks#4504).

RED-BEFORE: every test here fails against `DateTime(timezone=True)` on SQLite —
reads come back naive, so the comparison in `test_read_back_is_aware` raises
TypeError, and `test_zone_is_normalised_not_ignored` stores the wrong instant.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.exc import StatementError

from aiko_gateway.domain.models import SocialNonce, _utcnow
from aiko_gateway.domain.types import NaiveDatetimeRejected


def _nonce(name: str, expires_at: dt.datetime) -> SocialNonce:
    return SocialNonce(nonce=name, expires_at=expires_at, consumed=False)


@pytest.mark.asyncio
async def test_read_back_is_aware(session):
    """The defect itself: a stored aware value must not return naive.

    Without the type this raises `TypeError: can't compare offset-naive and
    offset-aware datetimes` on the final assert — which is exactly the crash
    that reached production in the token reaper (#4486).
    """
    session.add(_nonce("aware-roundtrip", _utcnow() + dt.timedelta(minutes=5)))
    await session.commit()
    session.expunge_all()

    row = (await session.execute(
        select(SocialNonce).where(SocialNonce.nonce == "aware-roundtrip")
    )).scalar_one()

    assert row.expires_at.tzinfo is not None, "read back naive — the class is open"
    assert row.expires_at > _utcnow()


@pytest.mark.asyncio
async def test_zone_is_normalised_not_ignored(session):
    """Two aware values for the SAME INSTANT in different zones must compare equal.

    This is the invisible half. SQLAlchemy's SQLite bind processor formats the
    wall-clock fields and ignores tzinfo, so a +07 value and its UTC twin land as
    DIFFERENT strings seven hours apart — correct only while everything happens
    to be UTC, and nothing enforced that.
    """
    instant = dt.datetime(2026, 9, 20, 6, 30, tzinfo=dt.timezone.utc)
    in_bangkok = instant.astimezone(dt.timezone(dt.timedelta(hours=7)))
    assert in_bangkok == instant  # same instant, different wall clock

    session.add(_nonce("as-utc", instant))
    session.add(_nonce("as-bangkok", in_bangkok))
    await session.commit()
    session.expunge_all()

    rows = {
        r.nonce: r.expires_at
        for r in (await session.execute(select(SocialNonce))).scalars()
    }
    assert rows["as-utc"] == rows["as-bangkok"], (
        "the same instant stored in two zones came back as two different times"
    )


@pytest.mark.asyncio
async def test_naive_bind_is_rejected_at_the_write(session):
    """A naive value is refused loudly, naming the problem, at the WRITE.

    Not coerced: assuming 'naive means UTC' silently records a wrong instant the
    first time a caller holds a local wall clock. The catch belongs at the
    earliest point the caller still has the context to fix it.

    MEASURED SHAPE: SQLAlchemy wraps a bind-processor raise in
    ``StatementError``, so the rejection does NOT arrive as a bare
    ``NaiveDatetimeRejected`` — it is the ``__cause__``. Asserted explicitly so
    the wrapping is documented rather than rediscovered, and so a caller with a
    broad ``except TypeError`` is proven NOT to swallow it.
    """
    session.add(_nonce("naive", dt.datetime(2026, 9, 20, 6, 30)))
    with pytest.raises(StatementError) as caught:
        await session.commit()
    assert isinstance(caught.value.__cause__, NaiveDatetimeRejected)
    assert "naive datetime" in str(caught.value.__cause__)
    await session.rollback()


@pytest.mark.asyncio
async def test_none_passes_through(session):
    """A nullable deadline is a legitimate value, not a missing one."""
    from aiko_gateway.domain.models import User
    from aiko_gateway.domain.ids import new_ulid

    u = User(id=new_ulid(), username="tz-none", display_name="tz none",
             aiko_username="tz_none", handle_changed_at=None)
    session.add(u)
    await session.commit()
    session.expunge_all()

    row = (await session.execute(
        select(User).where(User.username == "tz-none")
    )).scalar_one()
    assert row.handle_changed_at is None
    assert row.created_at.tzinfo is not None
