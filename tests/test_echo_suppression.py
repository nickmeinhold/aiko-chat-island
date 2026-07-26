"""Echo suppression correctness — the (channel, username, body) dedupe key.

The gateway records each of its own publishes so the aiko ChatServer's echo of
that publish is dropped on ingest (not re-persisted). The key is content:
`(aiko_channel, aiko_username, body)`.

REGRESSION (the duplicate-body bug): two IDENTICAL messages from the same user
in the same channel within the TTL window collapse onto ONE key. If a single
`mark_sent` call can only ever suppress a single echo, the second real send's
echo is treated as external and re-persisted + re-fanned-out — the user sees
their own message twice. Each outstanding publish must suppress exactly one
echo, so N identical sends must consume N echoes.

This is a stopgap hardening of the content-keyed scheme; the structural fix is
to carry a real message id across the bus (task #42, aiko_chat discussion #9)
and dedupe on identity instead of content.
"""
from __future__ import annotations

from aiko_gateway.domain import echo


def _clear() -> None:
    echo._seen.clear()


def test_single_send_suppresses_its_echo():
    _clear()
    echo.mark_sent("general", "alice", "hi")
    assert echo.is_own_echo("general", "alice", "hi") is True
    # The echo is consumed once; a second inbound with the same content is a
    # genuinely new message and must NOT be suppressed.
    assert echo.is_own_echo("general", "alice", "hi") is False


def test_two_identical_sends_suppress_two_echoes():
    """The duplicate-body regression: two sends -> two echoes dropped."""
    _clear()
    echo.mark_sent("general", "alice", "ok")
    echo.mark_sent("general", "alice", "ok")
    # Both echoes come back; both must be recognised as our own.
    assert echo.is_own_echo("general", "alice", "ok") is True
    assert echo.is_own_echo("general", "alice", "ok") is True
    # A third identical inbound is a real new message -> not suppressed.
    assert echo.is_own_echo("general", "alice", "ok") is False


def test_distinct_keys_are_independent():
    _clear()
    echo.mark_sent("general", "alice", "hi")
    echo.mark_sent("random", "alice", "hi")
    assert echo.is_own_echo("random", "alice", "hi") is True
    assert echo.is_own_echo("general", "alice", "hi") is True


def test_none_username_is_never_own_echo():
    _clear()
    echo.mark_sent("general", "alice", "hi")
    # A legacy bare-string inbound (username None) can never be our own publish.
    assert echo.is_own_echo("general", None, "hi") is False


def test_expired_marks_do_not_suppress(monkeypatch):
    _clear()
    t = {"now": 1000.0}
    monkeypatch.setattr(echo.time, "time", lambda: t["now"])
    echo.mark_sent("general", "alice", "hi")
    t["now"] += echo._TTL_SECONDS + 1.0
    # The mark has aged out; the inbound is treated as a fresh external message.
    assert echo.is_own_echo("general", "alice", "hi") is False
