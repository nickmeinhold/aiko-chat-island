"""Push reachability — an island that cannot reach its own devices says so (#3397).

THE STATE THIS EXISTS FOR. `push_service` gate 1 declines every wake when APNs is
unconfigured — correctly, because an operator who never set up push should not get
a crash. But the decline is silent and total, so an island holding registered
device tokens with no credentials is DEAF while every other signal reads healthy:
registration returns 201, the message persists, `/health` says ok, and the
recipient simply never hears anything.

That is not hypothetical. On 2026-08-23 a handset registered against enspyr (APNs
present-but-empty) while the credential lived on imagineering (zero tokens). Both
islands were internally correct; jointly the system was deaf and nothing said so.
Roughly four hours went into it, most of them spent reading a `0` from the wrong
island's `device_tokens` as a fact about the app rather than a fact about the query.

WHAT IS DELIBERATELY *NOT* HERE. There is no per-environment reachability, because
there is no such state: an APNs auth key (`.p8`) is environment-AGNOSTIC — the same
key authenticates against both hosts, proven 2026-08-23 — so a configured island can
reach a sandbox token and a production token alike. Reporting "reachable for
sandbox" separately would be a mechanism for a condition that cannot occur.

WHAT IS NOW HERE, AND WHY THE ARGUMENT ABOVE DOES NOT COVER IT. Per-PLATFORM
reachability is a genuinely different question from per-ENVIRONMENT, and the
`.p8`-is-environment-agnostic reasoning says nothing about it. With two
transports the old report lied in BOTH directions: an APNs-configured island
counted its Android rows as REACHABLE (the #3397 failure in a new direction), and
an FCM-only island would report every row unreachable AND fire the boot warning
on every healthy boot — the warning-nobody-reads that
`test_startup_is_silent_when_there_is_nothing_to_say` exists to prevent.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from pydantic import ValidationError

from aiko_gateway.config import Settings, settings
from aiko_gateway.domain import push_service, users_service
from aiko_gateway.domain.models import DeviceToken, Platform


async def _user_with_devices(session, n: int):
    user = await users_service.create_user(
        session, username="alice", display_name="Alice", password="pw")
    for i in range(n):
        session.add(DeviceToken(user_id=user.id, platform="apns",
                                token=f"{i}" * 64, apns_environment="sandbox"))
    await session.commit()
    return user


FCM_CREDENTIAL = (
    '{"type":"service_account","project_id":"aiko-island-test",'
    '"private_key_id":"0123456789abcdef",'
    '"private_key":"-----BEGIN PRIVATE KEY-----\\nx\\n-----END PRIVATE KEY-----\\n",'
    '"client_email":"island@aiko-island-test.iam.gserviceaccount.com"}'
)


async def _user_with_apns(session, n: int, *, username: str = "ada"):
    """An APNs-row user, mirroring `_user_with_android` — needed so the APNs arm of
    warn_if_unreachable can be exercised, which round 5 showed nothing did."""
    user = await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")
    for i in range(n):
        session.add(DeviceToken(user_id=user.id, platform="apns",
                                token=f"apns-{username}-{i}" + "z" * 80))
    await session.commit()
    return user


async def _user_with_android(session, n: int, *, username: str = "bob"):
    user = await users_service.create_user(
        session, username=username, display_name=username.title(), password="pw")
    for i in range(n):
        session.add(DeviceToken(user_id=user.id, platform="fcm",
                                token=f"fcm-{username}-{i}" + "z" * 80))
    await session.commit()
    return user


@pytest.fixture
def configured(monkeypatch):
    """APNs configured, FCM not — the shape of both live islands today."""
    # All FIVE: apns_voip_topic is part of the credential set `is_configured()`
    # counts (Carnot, cage-match PR#172 r1), so a four-name fixture describes an
    # island that cannot boot.
    for k, v in (("apns_key_id", "ABCDE12345"), ("apns_team_id", "TEAMID1234"),
                 ("apns_topic", "cc.example.app"),
                 ("apns_voip_topic", "cc.example.app.voip"),
                 ("apns_private_key", "-----BEGIN PRIVATE KEY-----")):
        monkeypatch.setattr(settings, k, v, raising=False)
    monkeypatch.setattr(settings, "fcm_service_account_json", "", raising=False)


@pytest.fixture
def fcm_only(monkeypatch):
    """FCM configured, APNs not — a legitimate, bootable deployment (config.py's
    all-or-none guard makes APNs-absent a supported state)."""
    for k in ("apns_key_id", "apns_team_id", "apns_topic", "apns_private_key",
              "apns_voip_topic"):
        monkeypatch.setattr(settings, k, "", raising=False)
    monkeypatch.setattr(settings, "fcm_service_account_json", FCM_CREDENTIAL,
                        raising=False)


@pytest.fixture
def unconfigured(monkeypatch):
    for k in ("apns_key_id", "apns_team_id", "apns_topic", "apns_private_key",
              "apns_voip_topic"):
        monkeypatch.setattr(settings, k, "", raising=False)
    monkeypatch.setattr(settings, "fcm_service_account_json", "", raising=False)


# ------------------------------------------------------------------ the report

async def test_unconfigured_island_holding_tokens_reports_them_unreachable(
    session, unconfigured
):
    """The exact enspyr state. The count is what makes it actionable — "push is
    off" is a shrug, "push is off AND 2 devices are registered to it" is a bug."""
    await _user_with_devices(session, 2)
    report = await push_service.reachability(session)
    assert report == {"configured": False, "registered_devices": 2,
                      "unreachable_devices": 2,
                      "unreachable_by_platform": {"apns": 2}}


async def test_unconfigured_island_with_no_tokens_is_not_a_problem(
    session, unconfigured
):
    """Push simply not set up is a legitimate, intended state — most islands.
    It must NOT be reported as unreachable, or the signal becomes noise that
    every operator learns to ignore, which is worse than no signal."""
    report = await push_service.reachability(session)
    assert report == {"configured": False, "registered_devices": 0,
                      "unreachable_devices": 0, "unreachable_by_platform": {}}


async def test_configured_island_reaches_every_token_it_holds(session, configured):
    """Configured means reachable for EVERY token of that transport, sandbox and
    production alike: the .p8 authenticates against both hosts. There is no
    partial-ENVIRONMENT reachability state to report, and inventing one would be a
    mechanism for an impossible condition."""
    await _user_with_devices(session, 3)
    report = await push_service.reachability(session)
    assert report == {"configured": True, "registered_devices": 3,
                      "unreachable_devices": 0, "unreachable_by_platform": {}}


async def test_an_apns_island_counts_its_android_rows_as_unreachable(
    session, configured
):
    """THE #3397 FAILURE IN A NEW DIRECTION. Before this the count had NO
    platform predicate, so an APNs-configured island holding Android rows
    reported them REACHABLE — a deaf handset with every signal reading healthy,
    which is precisely the four-hour investigation this surface exists to end."""
    await _user_with_devices(session, 1)
    await _user_with_android(session, 2)
    report = await push_service.reachability(session)
    assert report["registered_devices"] == 3
    assert report["unreachable_devices"] == 2
    assert report["unreachable_by_platform"] == {"fcm": 2}


async def test_an_fcm_only_island_reaches_its_android_rows(session, fcm_only):
    """THE MIRROR, and the arm that stops "unreachable" collapsing back into
    "APNs is off". An FCM-only island reaching its Android handsets is a healthy
    island."""
    await _user_with_android(session, 2)
    report = await push_service.reachability(session)
    assert report == {"configured": True, "registered_devices": 2,
                      "unreachable_devices": 0, "unreachable_by_platform": {}}


# NO TEST FOR THE UNKNOWN-PLATFORM ARM, and the absence is deliberate.
# `reachability` fails CLOSED on a stored platform outside the enum (an unknown
# string counts as unreachable rather than reachable), but that state is
# UNREPRESENTABLE through the database: `ck_device_tokens_platform` is rendered
# FROM the enum by `_in_check`, so an INSERT or UPDATE carrying 'martian' is
# refused — verified, not assumed. The branch is reachable only if the enum
# SHRINKS in a later release while old rows persist.
#
# Kept in the code because it costs one `try` and guessing "reachable" for a
# device nothing can send to is the one direction this surface exists to prevent;
# given no test because a test that cannot create the failure cannot clear it —
# the same reasoning `push_service` applies to its `is_private` arm.


# ------------------------------------------------------- the startup log line

async def test_startup_warns_when_devices_are_unreachable(
    session, unconfigured, caplog
):
    """The signal that would have ended the 4-hour investigation at minute one.
    Asserted on the WARNING level and on the count, because a DEBUG line nobody
    greps is the same silence in a different costume."""
    await _user_with_devices(session, 1)
    with caplog.at_level("WARNING"):
        await push_service.warn_if_unreachable(session)
    assert any(r.levelname == "WARNING" and "UNREACHABLE" in r.message.upper()
               for r in caplog.records), caplog.text
    assert "1" in caplog.text


async def test_startup_is_silent_when_there_is_nothing_to_say(
    session, configured, caplog
):
    """A warning that fires on a healthy island is a warning nobody reads.
    Positive control for the test above: same call, same fixtures, must be QUIET —
    without this, the assertion above could pass on a function that always logs."""
    await _user_with_devices(session, 2)
    with caplog.at_level("WARNING"):
        await push_service.warn_if_unreachable(session)
    assert not [r for r in caplog.records if r.levelname == "WARNING"], caplog.text


async def test_an_fcm_only_island_with_android_rows_is_silent_at_boot(
    session, fcm_only, caplog
):
    """THE NULL ARM THAT MUST NOT REGRESS. A per-platform report computed the
    naive way — "configured" meaning APNs — would fire this warning on every boot
    of a perfectly healthy Android-serving island."""
    await _user_with_android(session, 3)
    with caplog.at_level("WARNING"):
        await push_service.warn_if_unreachable(session)
    assert not [r for r in caplog.records if r.levelname == "WARNING"], caplog.text


async def test_an_apns_island_with_android_rows_warns_and_names_fcm(
    session, configured, caplog
):
    """The message has to name the variable the operator must set AND the place
    it silently fails to arrive: `deploy/update.sh` pulls the image and never
    syncs the box's `docker-compose.yml` (#2301), so a value set in `.env` can be
    inert in production with nothing anywhere saying so. That clause turns a
    four-hour investigation into a grep, and it is the only mitigation available
    for the one deploy gap nothing mechanical closes."""
    await _user_with_android(session, 2)
    with caplog.at_level("WARNING"):
        await push_service.warn_if_unreachable(session)
    text = caplog.text
    assert "FCM_SERVICE_ACCOUNT_JSON" in text, text
    assert "APNS_KEY_ID" not in text, (
        "the APNs arm fired on an island whose APNs transport is healthy")
    # #2301 IS NO LONGER ASSERTED HERE, AND THAT IS THE FIX (Tesla, cage-match
    # PR#172 r5). The compose-forwarding advice is only true for a transport the
    # operator can actually provision. FCM is currently a BOOT REFUSAL, so telling
    # them to make compose forward it is telling them to crash-loop a live island —
    # and this assertion is what REQUIRED that sentence to be present in the FCM
    # warning, so the test was pinning the contradiction in place. The clause now
    # lives in the APNs remedy and is asserted there
    # (test_the_apns_remedy_still_carries_the_compose_warning), where it is true.
    assert "#2301" not in text, (
        "the FCM warning tells the operator to make compose forward a credential "
        f"the island refuses to boot with. Text: {text}")


# -------------------------------------------------------------------- /health

@pytest_asyncio.fixture
async def health_client(session):
    from aiko_gateway.rest.deps import get_session

    async def _override():
        yield session

    app = FastAPI()
    from aiko_gateway import main as main_mod
    app.add_api_route("/health", main_mod.health, methods=["GET"])
    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def test_health_reports_live_reachability_not_a_boot_snapshot(
    health_client, session, unconfigured
):
    """READ LIVE, never cached at boot (#3193 is the standing complaint that
    /health reports config INTENT rather than reality — do not add a third
    instance). A device registered AFTER startup must appear immediately; a
    value computed once at boot would answer "at boot", not "now", which is a
    different question from the one the operator is asking."""
    first = await health_client.get("/health")
    assert first.json()["push"] == {"configured": False,
                                    "devices_unreachable": False}
    await _user_with_devices(session, 1)
    second = await health_client.get("/health")
    assert second.json()["push"]["devices_unreachable"] is True, (
        "/health served a boot-time snapshot instead of live state")


async def test_health_keeps_its_existing_shape(health_client, unconfigured):
    """Additive only for the keys a consumer actually reads.

    The previous version of this docstring said "monitoring depends on these
    keys" and pinned `channels` on that basis. Measured: the only two consumers
    are the compose healthcheck (HTTP status alone) and `deploy/update.sh`,
    which parses `git_sha` and `ref` and nothing else. No monitoring read
    `channels`; the claim was prose, not a dependency.
    """
    body = (await health_client.get("/health")).json()
    assert body["status"] == "ok"
    assert "aiko_connected" in body
    assert "git_sha" in body["build"] and "ref" in body["build"]


async def test_health_does_not_publish_channel_names(health_client, unconfigured):
    """/health is unauthenticated, so anything in it is public.

    Channel names are what people talk about, and the island's standing position
    is that it declines to know or tell (claude-tasks#3769). This endpoint
    echoed `settings.aiko_channels` — config intent, never live state — which
    bought a debugging convenience nothing consumed at the price of publishing
    the names to anyone. Nick, 2026-09-05: "there's no need".

    This arm exists so the field cannot return by accident.
    """
    body = (await health_client.get("/health")).json()
    assert "channels" not in body, (
        "/health must not publish channel names — it is unauthenticated")


# --------------------------------------------- /health must not become fragile

async def test_health_survives_a_database_failure(session, unconfigured, caplog):
    """SELF-REVIEW FINDING. /health is the container's liveness probe (compose
    `curl -fsS http://127.0.0.1:8095/health`) AND deploy/update.sh's post-deploy
    verification. Before #3397 it touched no database, so a DB problem could not
    reach it; adding a COUNT gave a transient SQLite lock the power to mark the
    container unhealthy and to report a SUCCESSFUL deploy as failed.

    So the push block degrades and the endpoint still answers 200. Driven through
    a session that raises, because the claim is about the failure path and a test
    that cannot produce the failure cannot clear it."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from aiko_gateway import main as main_mod
    from aiko_gateway.rest.deps import get_session

    class _Exploding:
        async def execute(self, *a, **kw):
            raise RuntimeError("database is locked")

    async def _override():
        yield _Exploding()

    app = FastAPI()
    app.add_api_route("/health", main_mod.health, methods=["GET"])
    app.dependency_overrides[get_session] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/health")
    assert resp.status_code == 200, "a DB hiccup took down the liveness probe"
    body = resp.json()
    assert body["status"] == "ok"
    assert body["push"] == {"status": "unknown"}, (
        "a database failure must read as UNKNOWN, not as a false unreachable alarm")


async def test_health_does_not_publish_a_device_population(
    health_client, session, unconfigured
):
    """SELF-REVIEW FINDING. /health is public and unauthenticated. A live count
    of registered devices is user-adjacent data and does not belong on it — the
    actionable count goes to the boot log, where box access is the prerequisite.
    Asserted on the serialized body so a nested count cannot slip back in."""
    await _user_with_devices(session, 3)
    body = (await health_client.get("/health")).text
    assert "registered_devices" not in body
    assert "unreachable_devices" not in body
    assert '"3"' not in body and ": 3" not in body


_DEV_JWT_SECRET = "x" * 64


def _fcm_credential_for_collision() -> str:
    import json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()).decode()
    return json.dumps({"project_id": "p",
                       "client_email": "e@p.iam.gserviceaccount.com",
                       "private_key": pem})


@pytest.mark.asyncio
async def test_the_fcm_remedy_does_not_instruct_what_the_boot_guard_refuses(
    session, configured, caplog
):
    """THE COLLISION TEST (Tesla, cage-match PR#172 r4).

    Two facts were each pinned in isolation and never brought together:

      1. `warn_if_unreachable` must NAME FCM when an APNs island holds Android rows
         (test_an_apns_island_with_android_rows_warns_and_names_fcm)
      2. a well-formed FCM credential must REFUSE to boot
         (test_a_well_formed_fcm_credential_is_REFUSED_until_android_can_receive)

    Both green, and together they described a remedy that says "Set
    FCM_SERVICE_ACCOUNT_JSON" against a guard that refuses exactly that. Both live
    islands already hold Android rows, so that instruction prints on every boot; an
    operator obeying it writes the var, pulls, and crash-loops under
    `restart: always` with the island already down. There is no FCM preflight to
    catch it on the way in — the compose comment says so.

    A full green could not see it because no test read the two facts in the same
    breath. This one does: it asserts the remedy and the guard agree, so they cannot
    drift apart again without something going red.
    """
    # READS THE RENDERED WARNING, NOT THE DICT (Tesla, cage-match PR#172 r5).
    # The first version of this test read `_UNREACHABLE_REMEDY[FCM]` in isolation
    # — and the contradiction it was built to catch had moved into the TEMPLATE
    # around that string, which told every platform to "check compose actually
    # forwards it". So the collision test had the very isolation-blindness it
    # exists to prevent, and passed while the rendered sentence still instructed
    # an operator to brick a live island. Assert what the operator READS.
    await _user_with_android(session, 2)
    with caplog.at_level("WARNING"):
        await push_service.warn_if_unreachable(session)
    remedy = caplog.text

    # The guard must really refuse — proving this test is colliding two LIVE facts,
    # not asserting prose against a rule that has quietly been lifted.
    with pytest.raises(ValidationError):
        Settings(_env_file=None, environment="dev", jwt_secret=_DEV_JWT_SECRET,
                 fcm_service_account_json=_fcm_credential_for_collision())

    assert "Set FCM_SERVICE_ACCOUNT_JSON" not in remedy, (
        "the boot warning instructs the operator to set a credential the boot "
        f"guard refuses — following it bricks a live island. Remedy: {remedy!r}")
    assert "not available" in remedy.lower() or "do not set" in remedy.lower(), (
        f"the warning must tell the operator the transport is unavailable rather "
        f"than how to enable it. Rendered: {remedy!r}")
    assert "#2301" not in remedy, (
        "the rendered FCM warning still tells the operator to make compose forward "
        f"a credential the island refuses to boot with. Rendered: {remedy!r}")


@pytest.mark.asyncio
async def test_the_apns_remedy_still_carries_the_compose_warning(session, caplog,
                                                                 monkeypatch):
    """The #2301 clause must survive where it IS true — on the provisionable arm.

    Moving it out of the shared template could easily have deleted it everywhere,
    which would lose the only mitigation for the one deploy gap nothing mechanical
    closes: `update.sh` pulls the image and never syncs the box's compose, so a
    value set in `.env` can be inert in production with nothing saying so.

    This is the OTHER half of the class fix. Round 5 showed that repairing one arm
    and assuming the rest follows is exactly how this defect kept recurring — so
    both arms get an assertion, in opposite directions.
    """
    for k in ("apns_key_id", "apns_team_id", "apns_topic", "apns_private_key",
              "apns_voip_topic"):
        monkeypatch.setattr(settings, k, "", raising=False)
    await _user_with_apns(session, 2)
    with caplog.at_level("WARNING"):
        await push_service.warn_if_unreachable(session)
    text = caplog.text
    assert "APNS_KEY_ID" in text, text
    assert "#2301" in text, (
        "the compose-forwarding mitigation vanished from the arm where it applies "
        f"— it was moved out of the template and must land here. Text: {text}")
