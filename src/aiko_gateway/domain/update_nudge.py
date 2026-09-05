"""Tell the OPERATOR when a newer island release exists — nothing more.

WHY THIS EXISTS. An island is sovereign: nobody can push it an update, and there is
deliberately no registry of operators, no telemetry and no phone-home (the island
declines to know, and that cuts both ways — we do not get to learn who runs one).
The consequence, named rather than assumed away: **a security fix reaches a
third-party operator only if they choose to pull, and no mechanism exists by which
they could otherwise find out.** Someone could run a known-vulnerable gateway
indefinitely and neither side would ever know.

So this is the smallest thing that closes that gap without taking any authority:
the island looks up the published release list and *says something to its operator*.
It never pulls, never restarts, never writes. The operator remains the only actor.

WHY NOT `/health`. That endpoint is unauthenticated, and it is what a MONITOR reads,
not what an operator reads — putting the nudge there would reach nobody new. It is
also the wire, so a new key needs cross-repo agreement. Note that behind-ness is NOT
a secret we are keeping: `/health` already publishes `ref`, deliberately (see
build_info.py's DISCLOSURE note), so anyone can already diff it against the public
release list. The reason to stay out of `/health` is that it is the wrong AUDIENCE,
not that the fact is sensitive.

FAIL DIRECTION IS OPEN, ON PURPOSE. This is weak-signal capture, not a mutation:
a mutating verb must abort on doubt, but a nudge that cannot reach GitHub must never
delay or break a boot. Every failure path here degrades to silence plus a log line.
The cost of a missed nudge is a late upgrade; the cost of a fatal nudge is an island
that will not start because a third party had an outage.

THE DISCLOSURE THIS DOES ADD, stated honestly: an enabled check makes one outbound
request per interval to the release host, so that host learns this box runs an island
on a rough schedule. GitHub already learns the same thing from image pulls, and the
island's domain is public in DNS — but a periodic call is a heartbeat where a pull is
an event, and that is a real difference. It is why `off` is a first-class level.
"""
from __future__ import annotations

import logging
import re
from enum import StrEnum

log = logging.getLogger("aiko_gateway.update_nudge")

# The release list to consult. A fork's operator points this at their own repo; it is
# validated as https at settings level, never taken from a request.
DEFAULT_RELEASES_URL = (
    "https://api.github.com/repos/nickmeinhold/aiko-chat-island/releases/latest")

# `v0.9.4` and `0.9.4` both parse; anything else does not. Pre-release and build
# metadata (`0.9.4-rc1`, `1.0.0+deadbeef`) are deliberately NOT matched: comparing
# them correctly is real semver work, and a nudge that silently mis-orders a
# pre-release is worse than one that stays quiet. Unparseable => no nudge.
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class UpdateNudge(StrEnum):
    """How much of a version bump is worth telling the operator about.

    A closed set, so it is an Enum and not a `str` — a typo in the env var must be a
    boot-time refusal, not a silently-never-notifying island.

    `StrEnum`, matching IslandMode, and NOT the older `(str, Enum)` spelling: since
    3.11 the latter's `str()` renders `UpdateNudge.MAJOR` rather than `major`, which
    the compose/config default-parity invariant reads as drift. Caught by that test.
    """

    OFF = "off"        # no outbound request at all
    MAJOR = "major"    # breaking releases only
    MINOR = "minor"    # breaking + feature releases
    ALL = "all"        # every release, including patches


def parse_version(raw: str | None) -> tuple[int, int, int] | None:
    """`v0.9.4` -> (0, 9, 4). None for anything this cannot order with confidence."""
    if not raw:
        return None
    m = _SEMVER.match(raw.strip())
    if not m:
        return None
    return (int(m[1]), int(m[2]), int(m[3]))


def should_nudge(
    current: tuple[int, int, int] | None,
    latest: tuple[int, int, int] | None,
    level: UpdateNudge,
) -> bool:
    """Does `latest` clear the operator's chosen threshold against `current`?

    The comparison is on the FIELD THAT MOVED, not on overall ordering, which is the
    whole point of the levels: at `major`, 0.9.4 -> 0.10.0 is a newer version the
    operator has asked NOT to hear about, so "is latest greater" is the wrong test.
    Find the most significant field that differs and ask whether the chosen level
    admits it.

    Unknown either side, or `off`, is False — an island that cannot name its own
    version (a source checkout, where build_info is all-null) says nothing rather
    than guessing.
    """
    if level is UpdateNudge.OFF or current is None or latest is None:
        return False
    if latest <= current:
        return False
    if latest[0] != current[0]:
        return True                       # major bump: every level except off wants it
    if latest[1] != current[1]:
        return level in (UpdateNudge.MINOR, UpdateNudge.ALL)
    return level is UpdateNudge.ALL       # patch only


def render_nudge(current: str, latest: str, url: str) -> str:
    """The operator-facing line. It states what to do, because a notice nobody can
    act on is noise — and `update.sh` is the only supported path."""
    return (
        f"a newer island release is available: {latest} (this island runs {current}). "
        f"Release notes: {url} — to take it, bump ISLAND_VERSION in .env to "
        f"{latest.lstrip('v')} and run deploy/update.sh. This island will NOT update "
        f"itself. Set ISLAND_UPDATE_NUDGE=off to stop this check.")


async def check_once(client, current_ref: str | None, level: UpdateNudge,
                     url: str = DEFAULT_RELEASES_URL) -> str | None:
    """One check. Returns the operator-facing line, or None for "nothing to say".

    Never raises: every failure — network, HTTP status, malformed JSON, a tag this
    cannot parse — is silence plus a debug line, per this module's fail-open contract.
    """
    if level is UpdateNudge.OFF:
        return None
    current = parse_version(current_ref)
    if current is None:
        log.debug("update nudge: this island cannot name its own version; skipping")
        return None
    try:
        resp = await client.get(url, headers={"Accept": "application/vnd.github+json"},
                                timeout=10.0)
        if resp.status_code != 200:
            log.debug("update nudge: release lookup returned %s", resp.status_code)
            return None
        body = resp.json()
        tag = body.get("tag_name")
        html_url = body.get("html_url") or url
    except Exception as exc:                      # noqa: BLE001 — fail open, always
        log.debug("update nudge: release lookup failed (%s)", exc.__class__.__name__)
        return None
    latest = parse_version(tag)
    if latest is None:
        log.debug("update nudge: unparseable upstream tag %r", tag)
        return None
    if not should_nudge(current, latest, level):
        return None
    return render_nudge(current_ref or "unknown", tag, html_url)
