"""Build provenance — an island can say what code it is running (#3792 sibling).

THE STATE THIS EXISTS FOR. `ISLAND_VERSION` lives only in the box's compose
environment; it is never read by the application and never surfaced. So nothing
served by a running island — not `/health`, not the signed manifest, not
`/v1/islands` — names the code inside it. "Which version is actually on that box"
has only ever been answerable by SSHing to the box and reading a file that the
deploy is not guaranteed to have updated: `deploy/update.sh` pulls the image and
does NOT sync `docker-compose.yml` (#2301), and on 2026-08-29 the deploy tree was
found stale from 2 August and had to be hand-refreshed before v0.9.1 could ship.

WHY A FILE AND NOT AN ENV VAR. Provenance the box can set is worthless — that is
the #2301 failure exactly. `ENV`/compose `environment:` is the OPERATOR's surface;
a file written into an image layer at build time is the BUILD's surface. It cannot
be changed by editing `.env` or compose, only by rebuilding (or deliberately
mounting over it). So the report describes the artifact rather than the host's
belief about the artifact.

WHAT THIS DELIBERATELY IS NOT. It is NOT attested. The block rides `/health`,
which is public, unauthenticated and unsigned, and a lying operator can trivially
bake a false sha. Attestable provenance means putting it inside the SIGNED
manifest, which `island_identity.MANIFEST_KEYS` makes an exact-set structural
reject — an extra key fails verification outright — so it is a `V` bump, and that
bump should be spent once, carrying #3731's media-posture split too. The
last test in this file is the guard that keeps the two apart until then.

The audience for the unsigned form is the operator auditing their OWN island,
where the threat model is a botched deploy, not a hostile host.
"""
from __future__ import annotations

import json

import pytest

from aiko_gateway import build_info, main


# --- the honest-unbaked case (the common one: local dev, source installs) ---- #

def test_absent_file_reports_every_field_none(tmp_path):
    """A source checkout has no baked file. It must say so with `null`, never a
    placeholder like "unknown"/"dev" — a fabricated value is worse than no value,
    because it reads as an answer."""
    assert build_info.read_build_info(tmp_path / "nope.json") == {
        "git_sha": None, "ref": None, "built_at": None,
        "aiko_services_ref": None, "aiko_chat_ref": None,
    }


def test_malformed_json_reports_none_and_never_raises(tmp_path):
    """/health is the container liveness probe AND deploy/update.sh's post-deploy
    verification. A corrupt provenance file must degrade to `null`, never turn a
    healthy island into a failed deploy."""
    p = tmp_path / "build-info.json"
    p.write_text("{not json at all")
    assert all(v is None for v in build_info.read_build_info(p).values())


def test_non_object_json_reports_none(tmp_path):
    p = tmp_path / "build-info.json"
    p.write_text('["a list is not a build report"]')
    assert all(v is None for v in build_info.read_build_info(p).values())


# --- the baked case ---------------------------------------------------------- #

def test_baked_values_are_echoed_verbatim(tmp_path):
    p = tmp_path / "build-info.json"
    baked = {"git_sha": "e356f2e" * 5 + "abcde", "ref": "v0.9.1",
             "built_at": "2026-09-01T07:49:00Z",
             "aiko_services_ref": "a66424db76c5bf8f11adfed456cf3a135baf7494",
             "aiko_chat_ref": "3e4e822b65b7e222920642c420661fb0c1e93bb6"}
    p.write_text(json.dumps(baked))
    assert build_info.read_build_info(p) == baked


def test_partial_file_fills_the_rest_with_none(tmp_path):
    """The shape is FIXED. A consumer reads five keys whether or not the builder
    supplied five, so a missing field is `null` rather than an absent key."""
    p = tmp_path / "build-info.json"
    p.write_text(json.dumps({"git_sha": "abc123"}))
    got = build_info.read_build_info(p)
    assert got["git_sha"] == "abc123"
    assert got["ref"] is None and got["built_at"] is None


def test_unknown_keys_are_dropped(tmp_path):
    """Fixed shape, not a passthrough: a build arg must not be able to add
    arbitrary keys to a public endpoint's response."""
    p = tmp_path / "build-info.json"
    p.write_text(json.dumps({"git_sha": "abc", "jwt_secret": "leaked"}))
    got = build_info.read_build_info(p)
    assert set(got) == set(build_info.FIELDS)
    assert "jwt_secret" not in got


def test_empty_string_is_null_not_empty(tmp_path):
    """THE REAL UNSET PATH, not an exotic one. The Dockerfile ARGs default to empty,
    so a plain `docker build` with no --build-arg bakes `"git_sha": ""`. That must
    read as "not known", exactly like an absent file — an empty string rendered into
    a deploy check would otherwise look like a successfully-read value."""
    p = tmp_path / "build-info.json"
    p.write_text(json.dumps(dict.fromkeys(build_info.FIELDS, "")))
    assert all(v is None for v in build_info.read_build_info(p).values())


@pytest.mark.parametrize("bad", [12345, True, None, {"a": 1}, ["x"]])
def test_non_string_values_become_none(tmp_path, bad):
    p = tmp_path / "build-info.json"
    p.write_text(json.dumps({"git_sha": bad}))
    assert build_info.read_build_info(p)["git_sha"] is None


def test_oversized_value_is_refused_not_truncated(tmp_path):
    """Bounded echo onto a public endpoint. Refused rather than truncated: a
    truncated sha still LOOKS like a sha and would be trusted as one."""
    p = tmp_path / "build-info.json"
    p.write_text(json.dumps({"ref": "x" * (build_info.MAX_VALUE_LEN + 1)}))
    assert build_info.read_build_info(p)["ref"] is None


def test_value_at_the_cap_is_kept(tmp_path):
    p = tmp_path / "build-info.json"
    p.write_text(json.dumps({"ref": "x" * build_info.MAX_VALUE_LEN}))
    assert build_info.read_build_info(p)["ref"] == "x" * build_info.MAX_VALUE_LEN


# --- the endpoint ------------------------------------------------------------ #

async def test_health_carries_the_build_block(session, monkeypatch):
    baked = dict.fromkeys(build_info.FIELDS, None) | {"git_sha": "deadbeef"}
    monkeypatch.setattr(build_info, "BUILD_INFO", baked)
    report = await main.health(session)
    assert report["build"] == baked
    assert report["status"] == "ok"   # the block is additive, nothing displaced


async def test_health_build_block_is_a_copy_not_the_module_state(session):
    """/health must not hand out a mutable reference to process-wide state."""
    report = await main.health(session)
    report["build"]["git_sha"] = "tampered"
    assert build_info.BUILD_INFO["git_sha"] != "tampered"


# --- the guard: provenance must NOT leak into the signed manifest ------------ #

async def test_build_info_is_absent_from_the_signed_manifest():
    """THE NEGATIVE CONTROL, and the reason this file is worth reading.

    `/health` provenance is UNSIGNED. The manifest is a trust document whose
    verifier compares the key set to `MANIFEST_KEYS` EXACTLY, so an extra key is a
    structural verification failure on every peer — a well-meaning "surface the
    version here too" would break federation, silently on our side and loudly on
    theirs. It would also place unsigned data inside a signed envelope, the exact
    authenticity smear `peers_service` already refuses for a peer's `mode`.

    This test goes red the moment someone adds it to the wrong endpoint."""
    from httpx import ASGITransport, AsyncClient

    from aiko_gateway.domain import island_identity as ii
    from aiko_gateway.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        m = (await c.get("/v1/island")).json()

    assert set(m) == set(ii.MANIFEST_KEYS), "manifest key set changed"
    assert not set(m) & set(build_info.FIELDS)
    assert "build" not in m
    assert m["v"] == 2, "a manifest field change is a V bump, never a silent add"
    assert ii.verify_manifest(m) is True
