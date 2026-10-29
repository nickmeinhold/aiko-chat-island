"""ULID minting — 128-bit, lexicographically time-sortable identifiers.

A ULID is 48 bits of millisecond timestamp + 80 bits of randomness, encoded as
26 Crockford-base32 chars. Because the time prefix sorts lexicographically, a
ULID doubles as the message ordering key AND the pagination cursor (plan §A1) —
no separate sequence needed. We mint our own (no dependency); monotonicity
within a millisecond is not required for ordering since the random tail breaks
ties deterministically per row.
"""
from __future__ import annotations

import os
import threading
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U
# A ULID as a SQLite GLOB (case-sensitive, whole-string): what `seed_axis` can decode.
ULID_GLOB = "[0-7]" + "[0-9A-HJKMNP-TV-Z]" * 25


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def new_ulid(ts_ms: int | None = None) -> str:
    """Return a fresh 26-char ULID. `ts_ms` overridable for tests."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    time_part = _encode(ts_ms, 10)              # 48 bits -> 10 chars
    rand_part = _encode(int.from_bytes(os.urandom(10), "big"), 16)  # 80 bits -> 16 chars
    return time_part + rand_part




def ulid_floor(ts_ms: int) -> str:
    """The smallest ULID minted at `ts_ms`, so `id >= ulid_floor(t)` selects every row
    minted at or after `t` using the primary key alone. A CANDIDATE bound only (the
    subscribe cap's `oldest_held`); the hold itself never reads time off an id."""
    return _encode(max(ts_ms, 0), 10) + "0" * 16


# THE MESSAGE AXIS IS MONOTONIC, AND IT IS ONLY AN ORDER. Messages and retractions
# share one id axis (cursors, fences, watermarks), and since design 21 the axis is also
# CAUSAL ORDER: "the recipient had posted before this invite" is `prior.id < invite.id`.
# `new_ulid`'s random tail makes two ids minted in one millisecond sort by chance, and
# a wall-clock step back makes a later id sort earlier, either of which let a reply
# minted AFTER an invite count as conduct BEFORE it (Carnot, cage-match #445 r2).
# Within the process (the gateway is single-worker, `worker_guard`; a shared axis is a
# precondition of lifting that) every axis id is strictly greater than the last: same
# or earlier millisecond -> last tail + 1. The tail's top bit starts clear, so
# increments have 2**79 of headroom.
#
# NOTHING READS TIME OFF THIS AXIS. Its timestamp is a sort key, and after a backward
# wall step it runs ahead of the wall. The hold reads `created_at` and the signature,
# both wall-clock readings (design 21 recast: fusing the order with a clock produced
# every clock finding in #445). Ids NOT on the axis (users, reports, recovery rows) keep
# `new_ulid`: there, an incrementing tail would only make the next id guessable.
_axis_lock = threading.Lock()
_axis_last = (0, 0)


def seed_axis(floor_ulid: str) -> None:
    """Start the axis above `floor_ulid` (the newest axis id already stored), so a
    clock that stepped back across a restart cannot mint below what exists."""
    global _axis_last
    if not floor_ulid:
        return
    ts = 0
    for ch in floor_ulid[:10]:
        ts = ts * 32 + _CROCKFORD.index(ch)
    rand = 0
    for ch in floor_ulid[10:26]:
        rand = rand * 32 + _CROCKFORD.index(ch)
    with _axis_lock:
        _axis_last = max(_axis_last, (ts, rand))


def next_axis_ulid() -> str:
    """A ULID strictly greater than every axis id this process has minted or seeded."""
    global _axis_last
    with _axis_lock:
        now = int(time.time() * 1000)
        last_ts, last_rand = _axis_last
        if now > last_ts:
            ts, rand = now, int.from_bytes(os.urandom(10), "big") >> 1
        elif last_rand + 1 < 1 << 80:
            ts, rand = last_ts, last_rand + 1
        else:
            ts, rand = last_ts + 1, 0
        _axis_last = (ts, rand)
    return _encode(ts, 10) + _encode(rand, 16)
