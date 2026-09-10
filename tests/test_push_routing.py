"""`plan_deliveries` — the routing table, swept rather than sampled.

WHY THIS FILE IS THE MERGE GATE. Measured: CI runs `pytest` and
`secrets-integrity` and nothing else — `pyproject.toml` declares no mypy, no
pyright, no ruff. So the `assert_never` calls in the router buy editor-time
errors and NOTHING in CI. The instrument that actually holds the closed sets
closed is a test that sweeps `itertools.product(Platform, TokenKind, WakeKind)`,
because a new enum member changes the sweep's input rather than requiring
somebody to remember this file exists.

`plan_deliveries` is pure and total on purpose: no session, no settings read, no
I/O, no await, and it MUST NEVER RAISE. `_wake_user` is called from a loop inside
`wake_for_message`'s single broad `try`, so a raise here abandons every remaining
RECIPIENT — not one device.
"""
from __future__ import annotations

import datetime as dt
import itertools

import pytest

from aiko_gateway.domain import push_service
from aiko_gateway.domain.models import (
    ApnsEnvironment, DeviceToken, Platform, TokenKind,
)
from aiko_gateway.domain.push_service import (
    ApnsDelivery, WakeKind, plan_deliveries,
)

# NAME KEPT, MEANING NARROWED: `Platform` still has two members (both islands
# hold Android rows) but only APNs has a send path, so 'both configured' now
# means 'every transport that CAN be configured'. The FCM member is skipped as
# NOT BUILT before the configured check ever runs — design 14 temper.
BOTH = frozenset(Platform)
NOW = dt.datetime(2026, 9, 9, 12, 0, tzinfo=dt.UTC)


def _row(row_id: str, platform: str, token_kind: str = TokenKind.ALERT.value,
         apns_environment: str = ApnsEnvironment.PRODUCTION.value) -> DeviceToken:
    """An unflushed row. Every column set explicitly — server_defaults do not
    apply to an instance that never reached the database."""
    return DeviceToken(id=row_id, user_id="01USER", platform=platform,
                       token=f"token-{row_id}", token_kind=token_kind,
                       apns_environment=apns_environment,
                       created_at=NOW, updated_at=NOW)


# ------------------------------------------------------------------ the sweep

# `[WakeKind.CALL_INVITE]`, NOT `WakeKind` — the docstring below explains why, and
# the two must not drift: a sweep over the whole enum would demand delivery for a
# future cancel and turn the router's deliberate skip into a red test.
@pytest.mark.parametrize(
    "platform,kind,wake",
    list(itertools.product([Platform.APNS], TokenKind, [WakeKind.CALL_INVITE])))
def test_every_platform_token_kind_combination_is_routed_for_call_invite(
        platform, kind, wake):
    """TOTALITY OVER THE ENUMS, so a member added without teaching the router
    about it fails HERE rather than at a handset that does not ring.

    SCOPED TO `CALL_INVITE`, DELIBERATELY (Carnot, cage-match PR#172 r6). This
    swept `product(Platform, TokenKind, WakeKind)` and REQUIRED a delivery for
    every member — which inverts its own purpose the moment `WakeKind` grows. The
    router is built so a future `CALL_END` cancel cannot silently inherit ring
    behaviour: both arms now match on `wake` and fall through to a named skip. A
    test demanding "exactly one delivery, zero skips" for EVERY WakeKind would fail
    that deliberate skip and read as a regression, so the instrument built to force
    a decision would instead have argued for shipping the silent inheritance.

    So this asserts the CURRENT matrix — every platform/kind pair, for the one wake
    kind that exists — and `test_a_new_wake_kind_must_be_routed_explicitly` below
    carries the future-proofing, by failing on an UNHANDLED kind rather than
    requiring delivery for all of them. Two different questions, two tests.
    """
    rows = [_row("01ROW", platform.value, kind.value)]
    deliveries, skips = plan_deliveries(rows, wake=wake, configured=BOTH)
    assert skips == [], f"{platform}/{kind}/{wake} was skipped"
    assert len(deliveries) == 1
    delivered = deliveries[0]
    assert delivered.row_id == "01ROW"
    if platform is Platform.APNS:
        assert isinstance(delivered, ApnsDelivery)
        assert delivered.token_kind is kind, (
            "an APNs delivery lost the row's kind — the header fork reads it")
    else:
        assert isinstance(delivered, FcmDelivery)


def test_a_new_wake_kind_must_be_routed_explicitly():
    """THE FUTURE-PROOFING, asked the right way round (Carnot, cage-match PR#172 r6).

    The question is NOT "does every WakeKind get delivered?" — that would bless a
    cancel arriving as a ring. It is "does an UNKNOWN WakeKind get silently
    delivered?", and the answer must be no, on BOTH transports.

    Round 2 found the router had this protection on the APNs arm only, so a future
    `CALL_END` would have been skipped on iOS pending a decision while Android sent
    a HIGH-priority data ring for a cancel. This drives a synthetic member through
    both arms and requires a NAMED SKIP, which is what forces the next author to
    make a decision rather than inherit one.
    """
    import enum

    class _FutureWake(enum.Enum):
        CALL_END = "call_end"

    for platform in [Platform.APNS]:   # the only platform with a send path
        rows = [_row("01ROW", platform.value, TokenKind.ALERT.value)]
        deliveries, skips = plan_deliveries(
            rows, wake=_FutureWake.CALL_END, configured=BOTH)
        assert deliveries == [], (
            f"{platform} DELIVERED an unrecognised wake kind — a future cancel "
            "would ring the handset it was meant to stop")
        assert len(skips) == 1 and skips[0][0] == "01ROW", (
            f"{platform} dropped an unrecognised wake kind without naming it; a "
            f"silent skip is how the decision gets missed. Got: {skips}")


# ------------------------------------------------------------------ the fanout

def test_a_handset_holding_both_kinds_yields_both_deliveries():
    """ARM (B), CHOSEN DELIBERATELY. Rows carry NO device identity — an alert
    token and a voip token for one phone are two unrelated strings, and design 12
    Decision 2a explicitly refuses to infer pairing — so "one push per handset"
    is NOT COMPUTABLE. That is a REPRESENTATION gap, not a tuning choice.

    Arm (A), voip-preferred, would silently never ring an alert-only SECOND Apple
    device (an iPad, an old phone) while any voip row exists: a MISSED CALL. Arm
    (B) costs a dual-registered iPhone a CallKit ring plus a redundant banner: a
    BLEMISH. This module already made that exact trade, in those words.
    """
    rows = [_row("01ALERT", Platform.APNS.value, TokenKind.ALERT.value),
            _row("01VOIP", Platform.APNS.value, TokenKind.VOIP.value)]
    deliveries, skips = plan_deliveries(rows, wake=WakeKind.CALL_INVITE,
                                        configured=BOTH)
    assert skips == []
    assert sorted(d.row_id for d in deliveries) == ["01ALERT", "01VOIP"]


# ---------------------------------------------------------------- the skips

def test_a_row_whose_transport_is_unconfigured_is_skipped_not_sent():
    """An OPERATOR-FIXABLE fact. The property under test is that a device which
    registered successfully and is never woken must not become indistinguishable
    from a delivery bug — so the row is skipped LOUDLY, with a reason naming
    something the operator can act on, rather than filtered out in SQL."""
    rows = [_row("01APNS", Platform.APNS.value),
            _row("01FCM", Platform.FCM.value)]
    deliveries, skips = plan_deliveries(
        rows, wake=WakeKind.CALL_INVITE, configured=frozenset({Platform.APNS}))
    assert [d.row_id for d in deliveries] == ["01APNS"]
    assert skips == [("01FCM", "transport_not_built")], (
        "an Android row must name NOT BUILT — `transport_not_configured` implies\n"
        "an operator setting that would fix it, and none exists (design 14 temper)")


def test_nothing_is_planned_when_no_transport_is_configured():
    """The whole-island arm. Not an error and not an exception — an island with
    no push credentials simply plans nothing."""
    rows = [_row("01APNS", Platform.APNS.value), _row("01FCM", Platform.FCM.value)]
    deliveries, skips = plan_deliveries(rows, wake=WakeKind.CALL_INVITE,
                                        configured=frozenset())
    assert deliveries == []
    assert sorted(skips) == [("01APNS", "transport_not_configured"),
                             ("01FCM", "transport_not_built")]


@pytest.mark.parametrize("bad", [
    {"platform": "martian"},
    {"token_kind": "shout"},
    {"apns_environment": "staging"},
])
def test_a_corrupt_row_skips_only_that_row_and_does_not_raise(bad):
    """PROVES `plan_deliveries` IS TOTAL. The original partitioning read RAW
    STRINGS, so a corrupt kind fell out of every bucket and vanished with no log
    line at all — the one remaining silent cell in a design named for having none.

    A raise here would abandon every remaining RECIPIENT, because `_wake_user`
    runs inside `wake_for_message`'s single broad try.
    """
    kwargs = {"platform": Platform.APNS.value}
    kwargs.update(bad)
    rows = [_row("01GOOD1", Platform.APNS.value),
            _row("01BAD", **kwargs),
            _row("01GOOD2", Platform.APNS.value)]
    deliveries, skips = plan_deliveries(rows, wake=WakeKind.CALL_INVITE,
                                        configured=BOTH)
    assert sorted(d.row_id for d in deliveries) == ["01GOOD1", "01GOOD2"]
    assert skips == [("01BAD", "unroutable_row")]


# ------------------------------------------------------ the config-probe registry

def test_every_platform_has_a_config_probe():
    """Belt-and-braces beside the import-time raise. A `Platform` member added
    without a probe would make `_configured_platforms()` silently treat it as
    unconfigured — every device on that transport skipped, forever, with a
    reason that reads like an operator's fault."""
    assert set(push_service._CONFIG_PROBES) == set(Platform)


def test_the_probe_registry_is_never_the_send_path():
    """A config-probe registry iterated to SEND would be a second door wearing a
    dict. Dispatch goes through the single `match` on the Delivery union and
    nowhere else — asserted structurally, because the property is about what the
    module does NOT contain."""
    import inspect
    source = inspect.getsource(push_service._wake_user)
    assert "_CONFIG_PROBES" not in source


@pytest.mark.parametrize("kind", list(TokenKind))
def test_an_android_row_is_skipped_as_NOT_BUILT_whatever_kind_it_carries(kind):
    """Android rows exist on both live islands and there is no send path for them.

    The reason must be `transport_not_built`, NOT `transport_not_configured`: the
    latter names an operator setting that would fix it, and none exists — design
    14's temper dissolved shipping an FCM send path ahead of the client's receive
    half, so there is no credential to set. A remedy an operator cannot act on is
    how two rounds of contradictory guidance happened.

    THE ARM THAT DISCRIMINATES: `configured=BOTH` — the not-built skip must fire
    even when the island is maximally configured, because it is a fact about what
    is BUILT, not about what is set.
    """
    deliveries, skips = plan_deliveries(
        [_row("01ROW", Platform.FCM.value, kind.value)],
        wake=WakeKind.CALL_INVITE, configured=BOTH)
    assert deliveries == [], "an Android row produced a delivery with no send path"
    assert skips == [("01ROW", "transport_not_built")], (
        f"an Android row must be named NOT BUILT, got {skips}")
