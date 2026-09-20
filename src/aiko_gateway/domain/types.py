"""Column types that make an illegal value unrepresentable (claude-tasks#4504).

``UtcDateTime`` is the only member so far. It exists because
``DateTime(timezone=True)`` is a LIE on SQLite, and SQLite is the sole engine in
dev AND prod (see CLAUDE.md).

THE MEASURED FACT. Every datetime this codebase constructs is aware
(``models._utcnow()`` is ``datetime.now(timezone.utc)``; there is no naive
construction anywhere in ``src/``). SQLite stores no zone, so every value read
back is NAIVE, and comparing one to a freshly-constructed aware value raises
``TypeError``. That is the crash fixed locally at six sites; this is the class.

THE SHARPER FACT, and the reason a per-site patch was never enough: SQLAlchemy's
SQLite ``DateTime`` bind processor IGNORES ``tzinfo`` and formats the wall-clock
fields. Measured::

    aware UTC                    -> '2026-09-16 09:31:36.811194'
    aware +07 (the SAME instant) -> '2026-09-16 16:31:36.811194'
    naive                        -> '2026-09-16 09:31:36.811194'

So pushing a comparison down into SQL — which is what
``synchronize_session=False`` at each patched site achieves — is correct ONLY
while every datetime in the system happens to be UTC. Nothing enforced that. The
sites were correct by coincidence, and the coincidence was invisible.

WHAT THIS TYPE DOES, both directions, so neither half can drift:

  * BIND — a naive value is REJECTED, loudly, at the write. An aware value is
    converted to UTC and stored with its zone dropped, so the stored digits are
    always UTC digits regardless of what zone the caller held.
  * RESULT — the read value gets ``tzinfo=UTC`` attached, so it comes back AWARE,
    matching what the codebase constructs. A comparison in Python can no longer
    raise.

WHY REJECT RATHER THAN COERCE a naive bind. Assuming "naive means UTC" would
silently write a wrong instant the first time someone hands us a local-zone
wall-clock, which is precisely the failure the measured table above describes.
Rejecting turns an invisible data defect into a loud error at the WRITE, instead
of a `TypeError` in some unrelated comparison months later.

SCOPE OF THE CATCH, stated precisely (Tesla, cage-match PR#185 r1). It fires at
BIND/FLUSH, not at object construction: ``SocialNonce(expires_at=<naive>)`` is
still representable and sits in the identity map until the write. An earlier
draft of this docstring said "construction", which promised a guarantee this type
does not provide — the outer refusals that ARE constructor-level live in the
domain objects (``ReapOrder`` raises on a zone-less instant). Two layers, and
this is the lower one.

NO MIGRATION. The underlying storage is unchanged — this is still a
``DateTime`` column holding the same ISO-ish text SQLite always held. Only the
Python-side round trip changes, so ``alembic heads`` is untouched (ISL-0001).

TIMEZONE=TRUE ON ``impl``: kept as the honest declaration of intent, and on
SQLite it is inert (which is the bug this type works around).

DO NOT READ IT AS PORTABILITY — an earlier draft of this paragraph claimed the
bind conversion would be "a no-op rather than a contradiction" on an engine that
respects the flag, and that is BACKWARDS (Tesla and Carnot, independently,
cage-match PR#185 r1). ``process_bind_param`` ends in ``replace(tzinfo=None)``,
so against a real ``timestamptz`` column it hands over naive digits that the
server then interprets in ITS session zone — the contradiction, not the no-op.
SQLite is the sole engine in dev and prod, so this tree is correct; the next
island that "just uses Postgres" must give this type dialect-specific bind
behaviour FIRST. Tracked rather than hand-waved.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator

__all__ = ["UtcDateTime"]


class NaiveDatetimeRejected(TypeError):
    """A naive datetime reached a ``UtcDateTime`` column.

    A ``TypeError`` subclass because that is what the un-typed code raised when
    the same mistake surfaced at comparison time — the kinship is the point.

    MEASURED, and NOT what a first draft of this docstring claimed: SQLAlchemy
    wraps a bind-processor exception in ``StatementError``, so a caller catching
    ``TypeError`` does NOT catch this. That is the better outcome — a broad
    ``except TypeError`` swallowing a bad write is exactly the paper-over this
    type exists to end — but it means the raise surfaces as a
    ``StatementError`` whose ``__cause__`` is this class. Tests assert on that
    shape (``test_naive_bind_is_rejected_at_the_write``); code should not try to
    catch it at all.
    """


class UtcDateTime(TypeDecorator):
    """A ``DateTime`` column that is aware on both sides of the wire.

    Binds: aware -> UTC, zone dropped for storage. Naive -> rejected.
    Results: naive from the DB -> UTC-aware. ``None`` passes through untouched
    (a nullable deadline is a legitimate value, not a missing one).
    """

    impl = DateTime(timezone=True)
    # No Python-visible state, so SQLAlchemy may cache compiled statements
    # against this type. Omitting it is a silent per-statement perf loss plus a
    # warning, not a correctness issue — but there is no reason to take either.
    cache_ok = True

    # NO ``coerce_compared_value`` OVERRIDE — and the reason is worth the lines,
    # because two cage-match rounds were spent adding one and then narrowing it.
    #
    # Tesla (r2) reported that ``TypeDecorator``'s default DELEGATES this to
    # ``impl``, so a compared value would skip the decorator and SQLite would
    # format ``+07`` as ``13:00``. The behaviour was measured and found correct
    # (a ``+07`` fence binds ``06:00``), but the MECHANISM claim was taken on
    # trust and an override was added to "convert a measurement into a
    # guarantee". It converted nothing.
    #
    # GROUNDED AGAINST THE SOURCE (r3), which is where this should have started:
    #
    #     sqlalchemy/sql/type_api.py :: TypeDecorator.coerce_compared_value
    #         """... By default, returns self."""
    #         return self
    #
    # The default IS ``return self``. Compared values have always reached this
    # type; there was never a coincidence here to harden. The override was pure
    # restatement, and the r3 "narrowing" of it was a second no-op on top (its
    # ``super()`` call returns ``self`` as well). Both removed.
    #
    # The one REAL thing that came out of it is handled in ``process_bind_param``
    # below: because every RHS reaches us, a non-datetime comparand hits our bind
    # processor, and it used to die on ``AttributeError: 'date' object has no
    # attribute 'tzinfo'``. That is PRE-EXISTING — not a regression, since the
    # default always routed here — and it is now a named error instead.

    def process_bind_param(
        self, value: dt.datetime | None, dialect: object
    ) -> dt.datetime | None:
        if value is None:
            return None
        if not isinstance(value, dt.datetime):
            # Every comparand reaches us (TypeDecorator.coerce_compared_value
            # returns self by default), so a `date`, an ISO string or a unix
            # float lands here. Previously that died on AttributeError deep in
            # the tzinfo check — an error message strictly worse than the one a
            # plain DateTime column would have produced, raised by a type whose
            # entire job is to produce a BETTER one. Name it instead.
            raise NaiveDatetimeRejected(
                f"a {type(value).__name__} reached a UtcDateTime column or "
                "comparison; this type takes timezone-aware datetimes only "
                "(models._utcnow(), or datetime.now(timezone.utc))"
            )
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise NaiveDatetimeRejected(
                "a naive datetime reached a UtcDateTime column; construct it "
                "aware (models._utcnow(), or datetime.now(timezone.utc)) — "
                "storing it would record wall-clock digits whose zone is a guess"
            )
        return value.astimezone(dt.timezone.utc).replace(tzinfo=None)

    def process_result_value(
        self, value: dt.datetime | None, dialect: object
    ) -> dt.datetime | None:
        if value is None:
            return None
        # An engine that DID honour timezone=True hands back an aware value;
        # normalise it rather than assuming it is already UTC.
        if value.tzinfo is not None:
            return value.astimezone(dt.timezone.utc)
        return value.replace(tzinfo=dt.timezone.utc)
