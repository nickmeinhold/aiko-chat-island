"""Key-bound @-mention span carriage (#2632) — the gateway's carrier role.

The gateway SHAPE+caps-validates the client's ``mentions`` array at the trust
boundary, then persists + echoes it VERBATIM (messages.mentions -> message_view).
It never resolves a target, never re-derives the client's offset basis, never
rewrites a span. Grounded on the app tab's ADR-0004: a mention target keys off the
opaque identity (never a home-qualified string) and there is NO central directory
(so nothing here searches/resolves users).

Three properties, mirroring the message-signing carriage tests:
  1. the validator is fail-closed (malformed/oversized arrays rejected, never
     persisted), including the bool-as-int trap;
  2. carriage is faithful end-to-end (validate -> create_outbound -> message_view
     round-trips the spans byte-for-byte, omit-when-empty, first-write-wins on
     resend);
  3. bus-born messages never carry mentions.
"""
from __future__ import annotations

import pytest

from aiko_gateway.domain import mentions, messages_service
from aiko_gateway.domain.models import Channel, User


def _span(target_type="user", target_id="k" * 40, offset=3, length=6) -> dict:
    return {"target_type": target_type, "target_id": target_id,
            "offset": offset, "length": length}


# -- 1. validator is fail-closed ---------------------------------------------
def test_validate_mentions_none_is_legal():
    assert mentions.validate_mentions(None) is None


def test_validate_mentions_empty_list_is_empty_list():
    """An explicit empty array is legal and distinct from None — both make
    message_view omit the key (falsy), but the validator must not choke on []."""
    assert mentions.validate_mentions([]) == []


def test_validate_mentions_accepts_wellformed_multi_target():
    raw = [
        _span(target_type="user", target_id="alice-key", offset=0, length=6),
        _span(target_type="channel", target_id="0" * 26, offset=10, length=8),
        _span(target_type="everyone", target_id="0" * 26, offset=20, length=9),
    ]
    out = mentions.validate_mentions(raw)
    assert out == raw


@pytest.mark.parametrize("bad,desc", [
    ("not-a-list", "top-level must be a list"),
    ({"target_type": "user"}, "a dict is not a list of spans"),
    (123, "a number is not a list"),
    ([_span() for _ in range(65)], "over the 64-span cap"),
    ([123], "span must be an object"),
    ([{**_span(), "extra": "x"}], "unexpected extra key"),
    ([{"target_id": "k", "offset": 0, "length": 1}], "missing target_type"),
    ([_span(target_type="")], "empty target_type"),
    ([_span(target_type="Bad")], "target_type not a lowercase token"),
    ([_span(target_type="a b")], "target_type with a space"),
    ([_span(target_type="x" * 33)], "target_type over the length cap"),
    ([_span(target_id="")], "empty target_id"),
    ([_span(target_id=123)], "target_id not a string"),
    ([_span(target_id="x" * 129)], "target_id over the 128 cap"),
    ([_span(offset=-1)], "negative offset"),
    ([_span(offset="3")], "offset not an int"),
    ([_span(offset=True)], "offset bool-as-int (True == 1) must not pass"),
    ([_span(offset=1 << 21)], "offset over the sanity cap"),
    ([_span(length=0)], "zero length"),
    ([_span(length=-2)], "negative length"),
    ([_span(length=True)], "length bool-as-int must not pass"),
    ([_span(length=1 << 21)], "length over the sanity cap"),
])
def test_validate_mentions_rejects_malformed(bad, desc):
    with pytest.raises(mentions.MentionError):
        mentions.validate_mentions(bad)


def test_validate_mentions_returns_fresh_projection():
    """The returned spans are fresh dicts (not the caller's), so a later mutation
    of the inbound frame can't reach the persisted/echoed JSON — same guard
    validate_origin makes."""
    raw = [_span()]
    out = mentions.validate_mentions(raw)
    assert out == raw
    out[0]["target_id"] = "mutated"
    assert raw[0]["target_id"] != "mutated"  # input untouched
    # and the reverse: mutating the input after validation can't reach `out`
    raw2 = [_span()]
    out2 = mentions.validate_mentions(raw2)
    raw2[0]["offset"] = 999
    assert out2[0]["offset"] == 3


def test_validate_mentions_strips_unexpected_nothing_but_keeps_order():
    """Field order in the projection is stable regardless of input key order."""
    raw = [{"length": 6, "offset": 3, "target_id": "k", "target_type": "user"}]
    out = mentions.validate_mentions(raw)
    assert list(out[0].keys()) == ["target_type", "target_id", "offset", "length"]


def test_validate_mentions_dedupes_identical_spans():
    """64 identical spans collapse to one (a mention set is a set); the cap is not
    the only wall against duplicate-span bloat."""
    out = mentions.validate_mentions([_span(target_id="bob", offset=0, length=3)] * 5)
    assert out == [_span(target_id="bob", offset=0, length=3)]


def test_validate_mentions_keeps_same_target_distinct_offsets():
    """Same target at DIFFERENT offsets is legitimately distinct (mentioning one
    person twice) — dedup must not collapse these."""
    raw = [_span(target_id="bob", offset=0, length=3),
           _span(target_id="bob", offset=10, length=3)]
    assert mentions.validate_mentions(raw) == raw


@pytest.mark.parametrize("bad_id", ["has\nnewline", "tab\there", "ctrl\x00null", "esc\x1b["])
def test_validate_mentions_rejects_control_chars_in_target_id(bad_id):
    """target_id is opaque but not a smuggling channel — control/non-printable chars
    (log/UI injection) are rejected, mirroring origin's charset discipline."""
    with pytest.raises(mentions.MentionError):
        mentions.validate_mentions([_span(target_id=bad_id)])


# -- block gate: strip_blocked_user_mentions ---------------------------------
def test_strip_blocked_removes_user_span_targeting_blocked():
    spans = [_span(target_type="user", target_id="blocked-uid"),
             _span(target_type="user", target_id="ok-uid", offset=20)]
    out = mentions.strip_blocked_user_mentions(spans, {"blocked-uid"})
    assert out == [_span(target_type="user", target_id="ok-uid", offset=20)]


def test_strip_blocked_keeps_non_user_targets():
    """channel/everyone targets have no user to block against — never stripped even
    if their (channel) id happens to collide with a blocked user id set."""
    spans = [_span(target_type="channel", target_id="blocked-uid"),
             _span(target_type="everyone", target_id="blocked-uid", offset=9)]
    assert mentions.strip_blocked_user_mentions(spans, {"blocked-uid"}) == spans


def test_strip_blocked_collapses_all_stripped_to_none():
    """If every span was a blocked user mention, the result is None (nothing to
    store) — feeds the empty→NULL normalization cleanly, no stored []."""
    spans = [_span(target_type="user", target_id="blocked-uid")]
    assert mentions.strip_blocked_user_mentions(spans, {"blocked-uid"}) is None


def test_strip_blocked_passthrough_when_none_or_empty_block_set():
    assert mentions.strip_blocked_user_mentions(None, {"x"}) is None
    keep = [_span(target_type="user", target_id="ok")]
    assert mentions.strip_blocked_user_mentions(keep, set()) == keep


# -- 2. carriage is faithful end-to-end --------------------------------------
@pytest.mark.asyncio
async def test_mentions_roundtrip_through_message_view(session):
    channel = Channel(id="0" * 26, name="general", kind="standard", aiko_channel="general")
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add_all([channel, user])
    await session.commit()

    spans = mentions.validate_mentions([
        _span(target_type="user", target_id="bob-key", offset=3, length=4)])
    row, created = await messages_service.create_outbound(
        session, user=user, channel=channel, body="hi @bob there",
        client_msg_id="m1", mentions=spans)
    assert created
    view = messages_service.message_view(row)
    assert view["mentions"] == spans


@pytest.mark.asyncio
async def test_message_view_omits_mentions_when_absent(session):
    channel = Channel(id="0" * 26, name="general", kind="standard", aiko_channel="general")
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add_all([channel, user])
    await session.commit()
    row, _ = await messages_service.create_outbound(
        session, user=user, channel=channel, body="hi", client_msg_id="m1",
        mentions=None)
    assert "mentions" not in messages_service.message_view(row)


@pytest.mark.asyncio
async def test_message_view_omits_mentions_when_empty_list(session):
    """An empty spans list is stored but reads as 'no mentions' — the key is
    omitted, so an empty array never reaches the wire as `mentions: []`."""
    channel = Channel(id="0" * 26, name="general", kind="standard", aiko_channel="general")
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add_all([channel, user])
    await session.commit()
    row, _ = await messages_service.create_outbound(
        session, user=user, channel=channel, body="hi", client_msg_id="m1",
        mentions=[])
    assert row.mentions is None  # empty normalized to NULL at write (one representation)
    assert "mentions" not in messages_service.message_view(row)


@pytest.mark.asyncio
async def test_resend_keeps_first_mentions(session):
    """A resend (same client_msg_id) returns the FIRST row and does NOT overwrite
    its mentions — a confused/malicious retry can't mutate stored spans, the same
    idempotency contract origin has."""
    channel = Channel(id="0" * 26, name="general", kind="standard", aiko_channel="general")
    user = User(id="u" * 26, username="ada", display_name="Ada", aiko_username="ada")
    session.add_all([channel, user])
    await session.commit()

    first = mentions.validate_mentions([_span(target_id="bob-key")])
    second = mentions.validate_mentions([_span(target_id="eve-key")])
    row1, c1 = await messages_service.create_outbound(
        session, user=user, channel=channel, body="hi @bob",
        client_msg_id="m1", mentions=first)
    row2, c2 = await messages_service.create_outbound(
        session, user=user, channel=channel, body="hi @bob",
        client_msg_id="m1", mentions=second)
    assert c1 and not c2
    assert row2.id == row1.id
    assert row2.mentions == first  # first spans win; the retry's are discarded


# -- 3. bus-born messages never carry mentions -------------------------------
@pytest.mark.asyncio
async def test_bus_born_message_never_carries_mentions(session):
    from aiko_gateway.aiko.payload import InboundMessage
    msg = InboundMessage(username="someone", channel="general",
                         timestamp=1720000000.0, message="from the bus", raw="{}")
    row = await messages_service.persist_inbound(session, msg)
    assert row is not None and row.mentions is None
    assert "mentions" not in messages_service.message_view(row)
