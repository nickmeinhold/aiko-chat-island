"""Live gateway<->aiko-bus round-trip — the regression net for task #45.

The gateway's entire bus surface is `AikoBusClient` (src/aiko_gateway/aiko/
client.py) + the `payload` codec. Every other test in the suite points at a
DEAD broker (ENVIRONMENT=test, AIKO_MQTT_HOST=127.0.0.1) and therefore only
exercises the graceful-degrade path. This test is the opposite: it stands up a
REAL broker + registrar + ChatServer and drives the actual `AikoBusClient`
through discover -> subscribe -> send -> receive, so a regression in the
discovery wiring or the wire codec fails CI instead of silently no-op'ing.

It deliberately tests the COMPONENT, not the full FastAPI app: persistence,
auth, and fanout are covered by the unit suite; the new-and-only thing a live
broker adds is the round-trip itself.

Stack bring-up reuses `spike/devstack.sh` (the single source of truth for the
HyperSpace bootstrap + ChatServer launch) rather than re-implementing it here —
that avoids the two-representations drift that has bitten this project before.

Lives under e2e/ (outside pytest `testpaths=["tests"]`) so the fast unit CI
never collects it; the dedicated bus-e2e workflow runs it explicitly. Local:
    pytest e2e/test_bus_roundtrip.py -s
(requires mosquitto on PATH and aiko_services + aiko_chat importable.)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DEVSTACK = REPO_ROOT / "spike" / "devstack.sh"

# The devstack broker lives on :1884 (clear of any stale default-port stack).
BUS_ENV = {
    "AIKO_MQTT_HOST": "localhost",
    "AIKO_MQTT_PORT": "1884",
    "AIKO_NAMESPACE": "aiko",
}

CHANNEL = "general"
# A per-process nonce makes our own publish unmistakable in the received stream.
NONCE = f"n{os.getpid()}-{int(time.time())}"


def _devstack(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(DEVSTACK), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture(scope="module")
def aiko_stack():
    """Bring up mosquitto + registrar + ChatServer for the module, tear down after."""
    _devstack("down")  # idempotent: clear any leftover stack from a prior run
    up = _devstack("up")
    if up.returncode != 0:
        pytest.fail(f"devstack up failed:\n{up.stdout}\n{up.stderr}")
    # Give the ChatServer a beat to register with the registrar before tests run.
    time.sleep(3)
    try:
        yield
    finally:
        _devstack("down")


def test_gateway_bus_roundtrip(aiko_stack):
    """AikoBusClient discovers the ChatServer, publishes, and receives its echo."""
    # aiko_services reads AIKO_MQTT_* from os.environ at compose time — set before import.
    os.environ.update(BUS_ENV)
    # Imported here (not at module top) so the env is in place first.
    from aiko_gateway.aiko.client import AikoBusClient

    received: list = []
    client = AikoBusClient([CHANNEL], received.append)
    client.start()

    # 1) Discovery: the gateway must find the ChatServer on the bus (the seam the
    #    earlier e2e could not verify — it ran with no broker).
    deadline = time.time() + 25
    while time.time() < deadline and not client.connected:
        time.sleep(0.2)
    assert client.connected, "AikoBusClient never discovered the ChatServer within 25s"

    # 2) Outbound: publish a nonce'd message as a non-gateway user.
    time.sleep(1.0)  # let the subscription settle
    body = f"bus-roundtrip {NONCE}"
    assert client.send("testbot", CHANNEL, body) is True, "send() returned False"

    # 3) Inbound: the ChatServer broadcasts it back; our on_message must receive
    #    it, parsed, with username + body preserved byte-exact.
    deadline = time.time() + 10
    mine = []
    while time.time() < deadline and not mine:
        mine = [m for m in received if NONCE in (m.message or "")]
        time.sleep(0.2)

    try:
        assert mine, (
            f"round-trip message never arrived. received={[m.message for m in received]}"
        )
        msg = mine[0]
        assert msg.username == "testbot", f"username not preserved: {msg.username!r}"
        assert msg.channel == CHANNEL, f"channel not preserved: {msg.channel!r}"
        assert msg.message == body, f"body not byte-exact: {msg.message!r}"
    finally:
        client.stop()
