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
    # ANCHOR TO THE ORIGINAL INSTANT, not just to each other (Tesla, cage-match
    # PR#185 r1). Pairwise equality alone would still pass if BOTH spellings were
    # mangled the same way — a check whose success value could coexist with the
    # defect. Comparing against `instant` is what makes it discriminating.
    assert rows["as-utc"] == instant, "round trip did not preserve the instant"
    assert rows["as-bangkok"] == instant, "the +07 spelling drifted off the instant"


@pytest.mark.asyncio
async def test_a_zoned_value_binds_as_utc_in_a_where_clause(session):
    """THE SHARPER HALF, at the type's own layer (Tesla, cage-match PR#185 r1).

    The test above covers INSERT and SELECT. The bug that actually reaped a live
    device was on a WHERE BIND — and that harmonic was asserted only over in
    `test_push_service.py`. If that test were ever simplified, the claim this
    change exists to make would go ungrounded while this file stayed green.

    A row expires at 06:30 UTC. We query `expires_at <= 06:00 UTC`, spelled
    `13:00+07`. The row must NOT match: 06:30 is after 06:00. Bind the wall clock
    instead of the instant and the comparison becomes `06:30 <= 13:00` — true —
    and the row is wrongly selected.
    """
    session.add(_nonce("where-bind", dt.datetime(2026, 9, 20, 6, 30, tzinfo=dt.timezone.utc)))
    await session.commit()

    fence_utc = dt.datetime(2026, 9, 20, 6, 0, tzinfo=dt.timezone.utc)
    fence_bangkok = fence_utc.astimezone(dt.timezone(dt.timedelta(hours=7)))
    assert fence_bangkok.hour == 13, "fixture: genuinely a different wall clock"

    for label, fence in (("utc", fence_utc), ("+07", fence_bangkok)):
        hit = (await session.execute(
            select(SocialNonce).where(
                SocialNonce.nonce == "where-bind",
                SocialNonce.expires_at <= fence,
            )
        )).scalars().all()
        assert hit == [], (
            f"the {label} spelling of the fence matched a row that expires AFTER it "
            "— the WHERE bind used the wall clock, not the instant"
        )

    # The discriminating other half: a fence that genuinely IS later must match,
    # or a WHERE that matches nothing would pass this test for the wrong reason.
    later = dt.datetime(2026, 9, 20, 7, 0, tzinfo=dt.timezone.utc).astimezone(
        dt.timezone(dt.timedelta(hours=7)))
    hit = (await session.execute(
        select(SocialNonce).where(
            SocialNonce.nonce == "where-bind",
            SocialNonce.expires_at <= later,
        )
    )).scalars().all()
    assert len(hit) == 1, "a genuinely-later fence must still match"


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


@pytest.mark.asyncio
async def test_the_capped_insert_select_path_stores_the_instant(session):
    """THE FUSE FOR `literal(now, UtcDateTime)` (Tesla, cage-match PR#185 r2).

    Round 1 closed a real bypass: `signing_keys_service._capped_insert` built its
    INSERT...SELECT with a bare `literal(now)`, which infers the GENERIC DateTime,
    so the row was written through SQLAlchemy's plain SQLite processor and the
    column type never saw the value — no UTC normalisation, no naive rejection,
    on the live send path.

    It was fixed with a comment and no test. Tesla: "Revert the second argument
    and this file stays green. The comment is a prophecy; a +07 `now` on the
    SELECT-list would make it a fuse." Exactly the class this PR is about — a fix
    whose absence nothing can detect.

    So: drive the real service with a `now` spelled +07. The stored instant must
    be the UTC one. Strip the type argument and the stored value moves seven
    hours and this fails.
    """
    from aiko_gateway.domain import signing_keys_service
    from aiko_gateway.domain.ids import new_ulid
    from aiko_gateway.domain.models import SigningKey, User

    user = User(id=new_ulid(), username="capped-insert", display_name="ci",
                aiko_username="capped_insert")
    session.add(user)
    await session.commit()

    instant = dt.datetime(2026, 9, 20, 6, 0, tzinfo=dt.timezone.utc)
    in_bangkok = instant.astimezone(dt.timezone(dt.timedelta(hours=7)))
    assert in_bangkok.hour == 13, "fixture: genuinely a different wall clock"

    stmt = signing_keys_service._capped_insert(
        key_id=new_ulid(), user_id=user.id, pubkey="z6MkTestKeyForCappedInsert",
        key_version=1, now=in_bangkok, max_keys=8)
    await session.execute(stmt)
    await session.commit()
    session.expunge_all()

    row = (await session.execute(
        select(SigningKey).where(SigningKey.user_id == user.id)
    )).scalar_one()
    assert row.first_seen_at == instant, (
        "the INSERT...SELECT stored the wall clock, not the instant — the "
        "literal() bind bypassed UtcDateTime"
    )
    assert row.last_seen_at == instant


@pytest.mark.asyncio
async def test_a_non_datetime_comparand_is_named_not_an_attributeerror(session):
    """`TypeDecorator.coerce_compared_value` returns self BY DEFAULT (SQLAlchemy
    source: "By default, returns self"), so EVERY comparand reaches this type —
    including a `date`, an ISO string, or a unix float.

    Pre-existing, not a regression: the default has always routed here. What was
    wrong is that they died on `AttributeError: 'date' object has no attribute
    'tzinfo'` — an error strictly worse than a plain DateTime column would give,
    raised by the type whose entire job is to give a better one.

    Two cage-match rounds (PR#185 r2, r3) were spent adding and then narrowing a
    `coerce_compared_value` override to "guarantee" routing that the documented
    default already guaranteed. Both were no-ops. This is what was actually left.
    """
    for bad in (dt.date(2026, 9, 20), "2026-09-20 06:00:00", 1789876800.0):
        with pytest.raises(StatementError) as caught:
            await session.execute(
                select(SocialNonce).where(SocialNonce.expires_at <= bad))
        assert isinstance(caught.value.__cause__, NaiveDatetimeRejected), (
            f"a {type(bad).__name__} comparand raised {caught.value.__cause__!r} "
            "rather than this type's named error")
        assert type(bad).__name__ in str(caught.value.__cause__), (
            "the error must name the offending type, or it is no better than "
            "the AttributeError it replaced")
        await session.rollback()
