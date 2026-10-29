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
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # excludes I, L, O, U


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


def ulid_ms(ulid: str) -> int:
    """The millisecond timestamp a ULID was minted at: the inverse of `new_ulid`'s
    time part. For a message row this is the island's RECEIPT instant, because
    `new_ulid()` runs at persist.

    Raises ValueError on a character outside the alphabet. Test fixtures that mint
    ids as zero-padded integers decode to a time near the epoch, which is the honest
    answer for them: such a row is "very old" to anything that reads time off its id.
    """
    value = 0
    for ch in ulid[:10]:
        value = value * 32 + _CROCKFORD.index(ch)
    return value


def ulid_floor(ts_ms: int) -> str:
    """The smallest ULID minted at `ts_ms`, so `id > ulid_floor(t)` selects every
    row persisted at or after `t` (to the millisecond) using the primary key alone."""
    return _encode(max(ts_ms, 0), 10) + "0" * 16
