"""The operator update nudge: level semantics, and the fail-open contract.

WHY THE TABLE IS A PRODUCT, not a list of cases. The bug this guards is a level that
notifies for everything (or nothing) while looking correct on the one example someone
wrote a test for. So every level is asserted against every KIND of bump, including the
ones it must stay silent about — a level with no silent case is not a level, it is a
boolean wearing four names.

The subtle one, and the reason `should_nudge` does not simply compare tuples: at
`major`, 0.9.4 -> 0.10.0 IS a newer version, and the operator has asked not to hear
about it. A greater-than test passes every example in this file except that one.
"""
from __future__ import annotations

import pytest

from aiko_gateway.domain.update_nudge import (
    UpdateNudge, check_once, parse_version, render_nudge, should_nudge)

MAJOR_BUMP = ((0, 9, 4), (1, 0, 0))
MINOR_BUMP = ((0, 9, 4), (0, 10, 0))
PATCH_BUMP = ((0, 9, 4), (0, 9, 5))

# level -> the bumps it MUST report, and the bumps it MUST stay silent about.
# Every level appears in both columns except the extremes, which is the point.
LEVELS = [
    (UpdateNudge.OFF,   [],                                    [MAJOR_BUMP, MINOR_BUMP, PATCH_BUMP]),
    (UpdateNudge.MAJOR, [MAJOR_BUMP],                          [MINOR_BUMP, PATCH_BUMP]),
    (UpdateNudge.MINOR, [MAJOR_BUMP, MINOR_BUMP],              [PATCH_BUMP]),
    (UpdateNudge.ALL,   [MAJOR_BUMP, MINOR_BUMP, PATCH_BUMP],  []),
]


@pytest.mark.parametrize("level,fires,silent", LEVELS)
def test_level_reports_exactly_what_it_promises(level, fires, silent):
    for current, latest in fires:
        assert should_nudge(current, latest, level) is True, (
            f"{level.value} must report {current} -> {latest}")
    for current, latest in silent:
        assert should_nudge(current, latest, level) is False, (
            f"{level.value} must stay SILENT about {current} -> {latest}")


@pytest.mark.parametrize("level", [UpdateNudge.MAJOR, UpdateNudge.MINOR, UpdateNudge.ALL])
def test_never_nudges_backwards_or_sideways(level):
    """An island ahead of the release list (a local build, a yanked release) must not
    be told to 'update' to something older than it is."""
    assert should_nudge((1, 0, 0), (0, 9, 4), level) is False
    assert should_nudge((0, 9, 4), (0, 9, 4), level) is False


@pytest.mark.parametrize("level", list(UpdateNudge))
def test_unknown_version_is_silence_not_a_guess(level):
    """A source checkout has all-null build info. Saying nothing is right; guessing
    a version and nudging against it would be a fabricated fact."""
    assert should_nudge(None, (9, 9, 9), level) is False
    assert should_nudge((0, 9, 4), None, level) is False


@pytest.mark.parametrize("raw,expected", [
    ("v0.9.4", (0, 9, 4)), ("0.9.4", (0, 9, 4)), ("  v1.20.300  ", (1, 20, 300)),
    ("v0.9.4-rc1", None),   # pre-release: ordering it correctly is real work
    ("1.0.0+abc", None),    # build metadata: same
    ("v1.0", None), ("latest", None), ("", None), (None, None),
])
def test_parse_version(raw, expected):
    assert parse_version(raw) == expected


def test_render_names_the_action_and_the_off_switch():
    line = render_nudge("v0.9.4", "v1.0.0", "https://example.test/r")
    for must in ("v1.0.0", "v0.9.4", "https://example.test/r",
                 "ISLAND_VERSION", "deploy/update.sh", "will NOT update itself",
                 "ISLAND_UPDATE_NUDGE=off"):
        assert must in line, f"the operator-facing line must name {must!r}"
    assert "1.0.0" in line and "ISLAND_VERSION in .env to 1.0.0" in line, (
        "the value to paste must be the IMAGE tag (no leading v), not the git tag")


# --------------------------------------------------------- the fail-open contract
#
# Each arm below is a way the outside world can fail. The nudge must degrade to
# silence for every one of them — never raise, because this runs in a boot-time task
# and a nudge that can break a boot is worse than no nudge at all.

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        if self._payload is _BAD_JSON:
            raise ValueError("not json")
        return self._payload


_BAD_JSON = object()


class _Client:
    def __init__(self, resp=None, exc=None):
        self._resp, self._exc = resp, exc
        self.calls = 0

    async def get(self, url, **kw):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._resp


async def test_happy_path_returns_the_operator_line():
    c = _Client(_Resp(payload={"tag_name": "v1.0.0", "html_url": "https://x.test/rel"}))
    line = await check_once(c, "v0.9.4", UpdateNudge.MAJOR)
    assert line is not None and "v1.0.0" in line and "https://x.test/rel" in line


@pytest.mark.parametrize("client,why", [
    (_Client(exc=OSError("dns")),                     "network down"),
    (_Client(exc=RuntimeError("boom")),               "any exception at all"),
    (_Client(_Resp(status=403)),                      "rate limited"),
    (_Client(_Resp(status=500)),                      "upstream broken"),
    (_Client(_Resp(payload=_BAD_JSON)),               "malformed body"),
    (_Client(_Resp(payload={})),                      "no tag_name"),
    (_Client(_Resp(payload={"tag_name": "nightly"})), "unparseable tag"),
])
async def test_every_upstream_failure_is_silence_not_an_exception(client, why):
    assert await check_once(client, "v0.9.4", UpdateNudge.MAJOR) is None, why


async def test_off_makes_no_request_at_all():
    """`off` must not merely discard the answer — the whole point is that the box
    stops talking to the release host, so the request must never be made."""
    c = _Client(_Resp(payload={"tag_name": "v99.0.0"}))
    assert await check_once(c, "v0.9.4", UpdateNudge.OFF) is None
    assert c.calls == 0, "off still contacted the release host"


async def test_unknown_own_version_makes_no_request():
    c = _Client(_Resp(payload={"tag_name": "v99.0.0"}))
    assert await check_once(c, None, UpdateNudge.ALL) is None
    assert c.calls == 0
