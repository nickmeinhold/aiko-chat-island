"""The STRUCTURAL tripwire for closed-set columns (#11 family).

WHY THIS FILE EXISTS, and it is not "more coverage". ``test_check_constraints.py``
is 700+ lines of hand-written, PER-COLUMN functional tests: it proves that the
constraints which exist do reject. It is *existentially enumerated* — it can only
speak about the columns somebody remembered to list — so a new closed-set column
that nobody wired to ``_in_check`` is not a failure there, it is an ABSENCE, and
an enumerative test cannot observe an absence.

That is exactly how ``messages.sender_kind`` got in. Its vocabulary was EMERGENT
from ``messages_service._kind_for``'s control flow (see the ``SenderKind``
docstring): two members borrowed from UserKind, two from ChannelKind, one bare
literal declared nowhere. A grep for the literal came back clean; the CHECK added
by 0027 found a sixth value an hour later. The repo's search instruments were
weaker than its schema, and nothing was watching the gap between them.

THE SHAPE OF THE FIX IS ALREADY IN THIS DIRECTORY. ``test_account_deletion_
cascade_guard.py`` states the principle in its own docstring — a STRUCTURAL
tripwire that introspects ``Base.metadata`` for every column matching a shape and
asserts the property holds, so a new column "fails first" — and then applies it
to FK-to-``users.id`` only. This file is that same instrument pointed at closed
sets. Keep both halves here too: this one catches a column's mere EXISTENCE,
``test_check_constraints.py`` proves the constraints behave.

WHY DETECTION IS BY COLUMN NAME AND NOT BY ENUM. The obvious design — "every
``StrEnum`` in the domain must be bound to a CHECK" — is the instrument that
cannot see either real escape: ``sender_kind`` had no enum (the set was control
flow) and ``message_reports.reason`` had no enum (the set was a bare tuple in
``moderation_service``). An enum-keyed check would have been green through both.
The naming shape is the signal that actually survives the failure mode, so that
is what is matched.

DELIBERATELY OVER-INCLUSIVE. ``_VOCABULARY_SUFFIXES`` matches more than it should
— ``passkey_challenges.state`` is a random nonce, not a vocabulary. That is the
right direction to err: a false positive costs one documented line in
``UNGUARDED_BY_DESIGN``, and writing that line forces someone to say out loud why
the column is not a closed set. A false negative costs another ``sender_kind``.
Every waiver carries its reason inline, so the allowlist is a record of decisions
rather than a list of exceptions.
"""
from __future__ import annotations

import re

from sqlalchemy import CheckConstraint, String

from aiko_gateway.domain.models import Base

# Name fragments that mark a column as holding a VOCABULARY rather than free text
# or an identifier. Drawn from the columns already guarded by _in_check (14 as of
# 0028)
# (kind/role/policy/category/visibility/platform/environment/resolution/operation)
# plus the shapes a future one is likely to take. Add to this, never trim it: the
# allowlist below is where a non-vocabulary column gets excused, by name and with
# a reason.
_VOCABULARY_SUFFIXES = (
    "kind", "role", "policy", "category", "visibility", "platform",
    "environment", "resolution", "operation", "reason", "status", "type",
    "state", "mode", "provider", "scheme", "level", "source", "tier",
)

# Columns whose NAME matches the vocabulary shape but which are NOT closed sets,
# or which are gated somewhere the schema cannot see. Each entry states which,
# because those are different claims with different failure modes: a genuinely
# open set can never regress, whereas a domain-layer gate is one refactor away
# from being the single-door fallacy again (cf. the v0.13.1 registration hole,
# where `open_registration` gated /register but never the passkey pair).
UNGUARDED_BY_DESIGN: dict[tuple[str, str], str] = {
    ("passkey_challenges", "state"): (
        "NOT A VOCABULARY. A random opaque nonce (security.py mints it); the "
        "name collides with the shape by accident."
    ),
    ("oauth_states", "provider"): (
        "GATED IN THE DOMAIN, not the schema. Written only from `prov.slug` "
        "after `oauth_broker.get_provider` resolves it against `_REGISTRY`, "
        "which raises BrokerUnknownProvider on a miss — fail-closed before the "
        "write. The set is operator-configurable by design (adding a broker is "
        "config + a registry entry), so pinning it in a CHECK would make the "
        "constraint narrower than the feature."
    ),
    ("social_identities", "provider"): (
        "GATED IN THE DOMAIN, not the schema. Every write path coerces through "
        "`oauth._as_provider`, which fails closed with UnknownProvider. The "
        "vocabulary IS declared (the `Provider` StrEnum), unlike the two real "
        "escapes this file exists to catch. A CHECK here would be defensible "
        "and is not claimed to be unnecessary — it is simply not the gap."
    ),
}


def _in_check_guarded() -> set[tuple[str, str]]:
    """Every (table, column) carrying an ``x IN (...)`` CHECK.

    Parsed back out of the compiled SQL rather than trusting the constraint NAME,
    because a name is a label someone typed and the SQL is what the DB enforces —
    `ck_messages_sender_kind` would satisfy a name-based check even if its
    sqltext had been edited to something inert.
    """
    guarded: set[tuple[str, str]] = set()
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, CheckConstraint):
                continue
            match = re.match(r"^(\w+) IN \(.+\)$", str(constraint.sqltext))
            if match:
                guarded.add((table.name, match.group(1)))
    return guarded


def _vocabulary_shaped() -> set[tuple[str, str]]:
    """Every string column whose name carries a vocabulary suffix."""
    found: set[tuple[str, str]] = set()
    for table in Base.metadata.tables.values():
        for col in table.columns:
            if not isinstance(col.type, String):
                continue
            name = col.name.lower()
            if any(name == s or name.endswith("_" + s) for s in _VOCABULARY_SUFFIXES):
                found.add((table.name, col.name))
    return found


def test_every_vocabulary_column_is_guarded_or_waived():
    """A vocabulary-shaped column must carry an IN-check or be waived BY NAME.

    This is the test that goes red when the next `sender_kind` lands. It does not
    assert the constraint works — `test_check_constraints.py` does that — only
    that the column has not entered the schema unobserved.
    """
    unaccounted = _vocabulary_shaped() - _in_check_guarded() - set(UNGUARDED_BY_DESIGN)
    assert not unaccounted, (
        "closed-set-shaped column(s) with no IN-check and no waiver: "
        + ", ".join(f"{t}.{c}" for t, c in sorted(unaccounted))
        + "\n\nEither give it a StrEnum + CheckConstraint(_in_check(...)) and an "
        "alembic revision (the 14 existing call sites are the pattern), or add it "
        "to UNGUARDED_BY_DESIGN with the reason it is not a closed set."
    )


def test_waivers_are_live_columns():
    """A waiver for a column that no longer exists is a stale excuse.

    Without this, dropping or renaming a column leaves its waiver behind, and the
    NEXT column to take that name inherits a pre-granted exemption it was never
    examined for — the allowlist silently widening to cover something nobody
    looked at.
    """
    live = {(t.name, c.name) for t in Base.metadata.tables.values() for c in t.columns}
    stale = set(UNGUARDED_BY_DESIGN) - live
    assert not stale, (
        "UNGUARDED_BY_DESIGN names column(s) that are not in the schema: "
        + ", ".join(f"{t}.{c}" for t, c in sorted(stale))
    )


def test_waivers_do_not_cover_guarded_columns():
    """A column must not be both CHECK-guarded and waived.

    If a waiver and a constraint ever describe the same column, one of them is a
    lie about the design — and the waiver's stated reason ("not a vocabulary",
    "gated in the domain") would be actively misleading beside a live CHECK.
    """
    both = set(UNGUARDED_BY_DESIGN) & _in_check_guarded()
    assert not both, (
        "column(s) both guarded and waived — delete the waiver: "
        + ", ".join(f"{t}.{c}" for t, c in sorted(both))
    )


def test_the_tripwire_can_actually_see_an_unguarded_column():
    """Positive control: the detector must fire on a known-unguarded shape.

    Without this, every assertion above passes vacuously the day `_in_check_
    guarded` or `_vocabulary_shaped` silently returns something wrong — a green
    suite that has stopped looking. This is the same trap the cascade guard names:
    a behavioural test alone passes vacuously for a table nobody seeded.
    """
    shaped = _vocabulary_shaped()
    # `messages.sender_kind` is the column this whole file is descended from, and
    # it IS guarded — so it must be detected as shaped AND as guarded.
    assert ("messages", "sender_kind") in shaped
    assert ("messages", "sender_kind") in _in_check_guarded()
    # And a column that is plainly not a vocabulary must not be detected.
    assert ("messages", "body") not in shaped
    assert ("users", "username") not in shaped


def test_the_detector_can_see_every_column_anyone_thought_to_guard():
    """Self-calibration: every IN-checked column must be vocabulary-SHAPED.

    This is the question the tripwire cannot otherwise ask about itself — "would
    I have seen this one if it had NOT been guarded?" A CHECK is evidence that a
    human judged that column a closed set. If the detector cannot see a column a
    human already classified, then `_VOCABULARY_SUFFIXES` is too narrow, and the
    NEXT column of that shape — arriving without a CHECK — passes unobserved.

    Measured 2026-09-26: 14 IN-checks, all 14 detected, zero blind spots. So a
    failure here is a real signal and not a known-tolerated gap: someone guarded
    a column whose name this file does not recognise (say `disposition` or
    `outcome`), and the fix is to ADD that fragment to _VOCABULARY_SUFFIXES —
    never to waive the column, which would be recording the detector's blindness
    as a property of the schema.
    """
    invisible = _in_check_guarded() - _vocabulary_shaped()
    assert not invisible, (
        "column(s) carry an IN-check but do not match _VOCABULARY_SUFFIXES, so "
        "this detector would MISS the next one of that shape: "
        + ", ".join(f"{t}.{c}" for t, c in sorted(invisible))
        + "\nAdd the missing name fragment to _VOCABULARY_SUFFIXES."
    )
