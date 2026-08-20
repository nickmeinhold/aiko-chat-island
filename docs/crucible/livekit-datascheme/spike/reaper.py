"""The seat reaper — the mechanism round 3 said `webrtc://` owes, now that it is mandatory.

## Why this exists (measured, not argued)

D1 races `disconnect()` against a deadline, abandons the await, and declares the Room dead.
`measure_seat.py` shows what the SFU thinks of that declaration:

| what happened to the client | seat freed after |
|---|---|
| clean `disconnect()` | 0.0 s |
| process SIGKILLed | 20.1 s |
| process SIGSTOPped (socket open, no answers) | 22.1 s |
| **Room abandoned, process still alive** | **never observed to free** |

LiveKit reaps a *silent* peer in ~20 s. But an abandoned Room inside a **living process** is
not silent: its native WebRTC threads keep answering keepalives, so the SFU sees a perfectly
healthy participant and the seat is held indefinitely. D1's ghost is therefore not a bounded
20-second nuisance — it is **immortal**, and it is worse than the sidecar orphan the design
rejected, because a sidecar orphan at least dies when something SIGKILLs it while this one is
actively kept alive by a healthy process.

That makes the reaper **not optional**. Without it, D1's fail-closed restart policy
(`(room, identity)` refuses a new bridge while a prior one is unreaped) denies the capability
**forever**, which is the self-DoS all four families predicted.

## What it does

`RemoveParticipant` on the room's control plane — the third door, next to "hung `disconnect()`"
and "wait for a timeout that will never come". It converts D1's outage into a reclaim.

## The trust question, named rather than assumed

The island has **never called the LiveKit server API**: `mint_room_token` deliberately withholds
`roomAdmin`/`roomList`, so this is new surface. Two consequences the design must own:

1. **Who holds the eviction power.** `RemoveParticipant` is authenticated with an API
   key/secret — i.e. **master credentials**, not a room-scoped join token. Whatever calls this
   is holding the keys to every room on the SFU. That is an argument for the **island** owning
   the reaper (it already holds those creds and is already the token authority under D3) and
   against handing it to every robot host.
2. **Who may evict whom.** Eviction must be scoped to *reclaiming your own identity's stale
   seat* — never "remove that participant". The rule below is deliberately narrow: a caller may
   only reap the identity it is about to mint for.

## Generational identity

Reaping alone still races: a reaped ghost could, in principle, be re-established by native
threads that have not noticed. So the reaper pairs with an **epoch** — the identity carries a
connect generation, and a new bridge mints epoch N+1 rather than reusing N. Epoch N's corpse
then cannot collide with N+1 even if the reap is slow or lost, and the reap becomes a
best-effort *cleanup* rather than a *precondition*. Belt and braces, because the failure mode
is silent.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from livekit import api

__all__ = ["SeatReaper", "epoch_identity", "base_identity", "ReapResult"]

# `<base>#<epoch>` — '#' is not produced by our ULID-based ids, so the split is unambiguous.
_EPOCH_RE = re.compile(r"^(?P<base>.+)#(?P<epoch>\d+)$")


def epoch_identity(base: str, epoch: int | None = None) -> str:
    """Build a generational identity. `epoch` defaults to a coarse monotonic stamp.

    The epoch exists so a new bridge NEVER has to wait for a previous one's corpse: epoch N+1
    is a different LiveKit identity from epoch N, so last-session-wins cannot fire between
    them and the fail-closed policy has nothing to fail closed against.
    """
    if epoch is None:
        epoch = int(time.time())
    return f"{base}#{epoch}"


def base_identity(identity: str) -> str:
    """Strip the epoch. `robot-7#1787226122` -> `robot-7`."""
    match = _EPOCH_RE.match(identity)
    return match.group("base") if match else identity


@dataclass
class ReapResult:
    room: str
    base: str
    evicted: list          # identities actually removed
    skipped: list          # seats seen but deliberately left alone
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class SeatReaper:
    """Reclaim stale seats for ONE base identity. Deliberately cannot do more than that.

    Not a general moderation tool: the only supported operation is "free the seats my own
    identity is still occupying so I can rejoin". Anything broader would put room-wide kick
    powers on whatever host runs a pipeline.
    """

    def __init__(self, url: str | None = None, api_key: str | None = None,
                 api_secret: str | None = None):
        url = url or os.environ["LK_URL"]
        # The server API is HTTP even when the client URL is a websocket one.
        self.http_url = url.replace("ws://", "http://").replace("wss://", "https://")
        self.api_key = api_key or os.environ["LK_API_KEY"]
        self.api_secret = api_secret or os.environ["LK_API_SECRET"]

    async def reap(self, room: str, base: str, keep: str | None = None) -> ReapResult:
        """Remove every seat in `room` whose base identity is `base`, except `keep`.

        `keep` is the identity we are ABOUT to use (the new epoch), so a reaper that runs
        after connecting cannot evict the bridge it was meant to protect. Returning rather
        than raising on a missing room is deliberate: "no room" and "no stale seat" are the
        same successful outcome to the caller.
        """
        evicted, skipped = [], []
        lk = api.LiveKitAPI(self.http_url, self.api_key, self.api_secret)
        try:
            listing = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
            for participant in listing.participants:
                identity = participant.identity
                if base_identity(identity) != base:
                    skipped.append(identity)      # someone else's seat — never ours to take
                    continue
                if keep is not None and identity == keep:
                    skipped.append(identity)
                    continue
                await lk.room.remove_participant(
                    api.RoomParticipantIdentity(room=room, identity=identity))
                evicted.append(identity)
            return ReapResult(room=room, base=base, evicted=evicted, skipped=skipped)
        except Exception as exc:
            # A room that does not exist yet is the common case on a first connect, and it is
            # a SUCCESS: there is no stale seat. Anything else is a real error the caller must
            # see — a reaper that silently swallows failures would reintroduce the immortal
            # ghost while reporting that it had cleaned up.
            text = repr(exc).lower()
            if "not found" in text or "does not exist" in text or "requested room" in text:
                return ReapResult(room=room, base=base, evicted=[], skipped=[])
            return ReapResult(room=room, base=base, evicted=evicted, skipped=skipped,
                              error=repr(exc))
        finally:
            await lk.aclose()
