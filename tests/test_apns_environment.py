"""Per-token APNs environment — one island serving dev builds AND TestFlight.

THE CAPABILITY, IN VERBS: a handset registers, naming the environment its build
was signed for; a wake for that handset reaches THAT environment's Apple host.
Two handsets on the same island, one sandbox and one production, both ring.

WHY A FLAG CANNOT DO THIS. An APNs auth key (.p8) is environment-agnostic — the
same key authenticates against api.sandbox.push.apple.com AND api.push.apple.com
(measured 2026-08-23: `BadDeviceToken` from both, i.e. auth accepted at each).
The environment lives in the DEVICE TOKEN: a token minted by a build entitled
`aps-environment: development` is valid ONLY at the sandbox host, and a
TestFlight/App Store build's token ONLY at production. Tokens are opaque and
carry no marking, so the server cannot infer it — the registrant must say.

`APNS_USE_SANDBOX` is therefore an island-wide answer to a per-device question,
and it is retained ONLY as the default for rows that predate this column or for
clients that do not send the field.
"""
from __future__ import annotations

import pytest

from aiko_gateway.config import settings
from aiko_gateway.domain import apns
from aiko_gateway.domain.models import ApnsEnvironment


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "apns_key_id", "ABCDE12345", raising=False)
    monkeypatch.setattr(settings, "apns_team_id", "TEAMID1234", raising=False)
    monkeypatch.setattr(settings, "apns_topic", "cc.example.app", raising=False)
    monkeypatch.setattr(settings, "apns_private_key", "-----BEGIN PRIVATE KEY-----",
                        raising=False)


# --- the host choice, which is the whole point -----------------------------

def test_a_sandbox_token_goes_to_the_sandbox_host(monkeypatch):
    monkeypatch.setattr(settings, "apns_use_sandbox", False, raising=False)
    assert apns.host_for(ApnsEnvironment.SANDBOX) == "https://api.sandbox.push.apple.com"


def test_a_production_token_goes_to_the_production_host(monkeypatch):
    """The island-wide flag says sandbox; this device says production. The DEVICE
    wins — that is the entire capability. A flag-only island cannot do this."""
    monkeypatch.setattr(settings, "apns_use_sandbox", True, raising=False)
    assert apns.host_for(ApnsEnvironment.PRODUCTION) == "https://api.push.apple.com"


@pytest.mark.parametrize("flag,expected", [
    (True, "https://api.sandbox.push.apple.com"),
    (False, "https://api.push.apple.com"),
])
def test_an_unstated_environment_falls_back_to_the_island_flag(monkeypatch, flag, expected):
    """Backward compatibility is the acceptance criterion here: every row that
    exists today has no environment, and every client shipping today sends none.
    Those MUST keep behaving exactly as they did before this column existed."""
    monkeypatch.setattr(settings, "apns_use_sandbox", flag, raising=False)
    assert apns.host_for(None) == expected


# --- the registration wire -------------------------------------------------

@pytest.mark.anyio
async def test_register_records_the_environment(session, user):
    from aiko_gateway.domain import devices_service as svc
    row = await svc.register_device(
        session, user_id=user.id, platform="apns", token="tok-sandbox-1",
        apns_environment=ApnsEnvironment.SANDBOX.value)
    assert row.apns_environment == "sandbox"


@pytest.mark.anyio
async def test_register_without_an_environment_stores_null(session, user):
    """An older client omits the field. NULL, not a guess — a guessed value is
    indistinguishable from a stated one at send time, and would silently pin a
    device to the wrong host forever."""
    from aiko_gateway.domain import devices_service as svc
    row = await svc.register_device(
        session, user_id=user.id, platform="apns", token="tok-legacy-1")
    assert row.apns_environment is None


@pytest.mark.anyio
async def test_reassignment_updates_the_environment(session, user, other_user):
    """The upsert reassigns a token that changed hands (app-repo design 15 leans
    on this). The NEW registrant's environment governs: the same handset can be
    reflashed from a dev build to TestFlight, and the row must follow."""
    from aiko_gateway.domain import devices_service as svc
    await svc.register_device(
        session, user_id=user.id, platform="apns", token="tok-handover",
        apns_environment=ApnsEnvironment.SANDBOX.value)
    row = await svc.register_device(
        session, user_id=other_user.id, platform="apns", token="tok-handover",
        apns_environment=ApnsEnvironment.PRODUCTION.value)
    assert row.user_id == other_user.id
    assert row.apns_environment == "production"


@pytest.mark.anyio
async def test_an_out_of_set_environment_is_rejected_at_the_boundary(client, auth_headers):
    """422 at the edge, not a 500 from the DB CHECK — the same discipline the
    `platform` field already uses."""
    r = await client.post("/v1/devices", headers=auth_headers,
                          json={"platform": "apns", "token": "tok-bad-env",
                                "apns_environment": "staging"})
    assert r.status_code == 422
