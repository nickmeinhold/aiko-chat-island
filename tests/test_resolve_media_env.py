"""Media hostname + credential-source resolution (PR#151's bug class).

Every finding across three cage-match rounds on PR#151 lived in standup.sh's media
wiring, and standup.sh cannot be tested — it needs a live Docker daemon and has no
dry-run. So four fixes shipped unguarded, and round 1's hostname-derivation bug came
back INSIDE round 3's fix. `deploy/resolve-media-env.sh` exists so the decision logic
can go red.

BOTH CONTROLS ARE BUILT HERE, following test_deploy_preflight.py. The arms that must
go red (a partial pair, two disagreeing pairs) matter more than the green ones,
because every failure mode of a wrong media resolution is SILENT: a gateway handed a
LIVEKIT_URL for a host that does not serve the SFU fails at CONNECT with no error
anywhere, and an invented hybrid credential authenticates nothing while looking
healthy on both sides.

The load-bearing test is `test_recorded_hostname_beats_convention_without_the_flag` —
that is the exact regression that shipped, and it is paired with a must-fail arm
proving the suite can still detect it.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "resolve-media-env.sh"

PAIR = {"LIVEKIT_API_KEY": "APIabc123", "LIVEKIT_API_SECRET": "s3cret-aaa"}
SFU_PAIR = {"LIVEKIT_API_KEY_ID": "APIabc123", "LIVEKIT_API_SECRET": "s3cret-aaa"}


def _write(path: Path, kv: dict[str, str]) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    return path


def _run(tmp_path, gw=None, lk=None, domain="example.org", turn_flag="", lk_flag=""):
    gw_file = _write(tmp_path / "gateway.env", gw) if gw is not None else tmp_path / "absent-gw.env"
    lk_file = _write(tmp_path / "livekit.env", lk) if lk is not None else tmp_path / "absent-lk.env"
    return subprocess.run(
        [str(SCRIPT), str(gw_file), str(lk_file), domain, turn_flag, lk_flag],
        capture_output=True, text=True)


def _parsed(result) -> dict[str, str]:
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


# --- hostname resolution order: flag > recorded > convention ----------------

def test_fresh_island_falls_back_to_convention(tmp_path) -> None:
    """No files, no flags — a first standup. Convention is the only source there is."""
    out = _parsed(_run(tmp_path))
    assert out["TURN_DOMAIN"] == "turn.example.org"
    assert out["LIVEKIT_DOMAIN"] == "livekit.example.org"
    assert out["CRED_SOURCE"] == "none"


def test_flags_win_over_everything(tmp_path) -> None:
    """An operator passing --livekit-domain means it, even against a recorded value."""
    out = _parsed(_run(
        tmp_path,
        lk={"LIVEKIT_TURN_DOMAIN": "recorded-turn.example.org",
            "LIVEKIT_DOMAIN": "recorded-sfu.example.org"},
        turn_flag="flag-turn.example.org", lk_flag="flag-sfu.example.org"))
    assert out["TURN_DOMAIN"] == "flag-turn.example.org"
    assert out["LIVEKIT_DOMAIN"] == "flag-sfu.example.org"


def test_recorded_hostname_beats_convention_without_the_flag(tmp_path) -> None:
    """THE REGRESSION THAT SHIPPED, and the reason this file exists.

    An operator stands up with --livekit-domain sfu.example.org, then re-runs WITH
    --with-media but WITHOUT repeating the flag. Before the class fix, both hostnames
    silently reset to convention and the .env write then OVERWROTE the recorded value
    with the convention one — destroying the only record of the operator's choice, on
    the invocation documented as safe to re-run. The gateway was handed a LIVEKIT_URL
    for a host that may not serve the SFU: fails at CONNECT, silently.

    Round 3 fixed this for the recovery branch only. This asserts the ORDER itself, so
    a future branch cannot re-acquire the bug by forgetting a rung."""
    out = _parsed(_run(
        tmp_path,
        lk={"LIVEKIT_TURN_DOMAIN": "turn-a.example.org",
            "LIVEKIT_DOMAIN": "sfu-a.example.org"}))
    assert out["LIVEKIT_DOMAIN"] == "sfu-a.example.org", "convention overwrote the recorded choice"
    assert out["TURN_DOMAIN"] == "turn-a.example.org", "TURN had the identical hole"


def test_the_regression_test_can_actually_fail(tmp_path) -> None:
    """MUST-FAIL ARM for the test above — a check whose outcome is independent of the
    thing it checks is not a check. With nothing recorded, the assertion in that test
    would be comparing convention against convention and could never go red. This
    pins that the recorded and convention values are genuinely DIFFERENT strings, so
    the assertion above discriminates."""
    out = _parsed(_run(tmp_path))
    assert out["LIVEKIT_DOMAIN"] == "livekit.example.org"
    assert out["LIVEKIT_DOMAIN"] != "sfu-a.example.org"


def test_turn_and_sfu_hostnames_are_separate_names(tmp_path) -> None:
    """PR#151 round 1's ship-blocker: an earlier draft set LIVEKIT_URL from the TURN
    hostname. Both live islands use livekit.<host> and turn.<host> as DISTINCT names,
    so conflating them hands every client the wrong websocket endpoint."""
    out = _parsed(_run(tmp_path, turn_flag="turn.example.org"))
    assert out["TURN_DOMAIN"] == "turn.example.org"
    assert out["LIVEKIT_DOMAIN"] == "livekit.example.org"
    assert out["LIVEKIT_DOMAIN"] != out["TURN_DOMAIN"]


# --- credential SOURCE (never the credential) -------------------------------

def test_sfu_env_alone_is_the_source(tmp_path) -> None:
    """Gateway .env deleted, bundled SFU env survived — round 2's torn plane. The pair
    must be recovered, not re-minted, or a re-run rotates a live media secret."""
    assert _parsed(_run(tmp_path, lk=SFU_PAIR))["CRED_SOURCE"] == "sfu"


def test_gateway_env_alone_is_the_source(tmp_path) -> None:
    """The other direction of the same torn plane (round 1)."""
    assert _parsed(_run(tmp_path, gw=PAIR))["CRED_SOURCE"] == "gateway"


def test_two_identical_complete_pairs_agree(tmp_path) -> None:
    """The healthy steady state of an island with media: both files, same pair."""
    assert _parsed(_run(tmp_path, gw=PAIR, lk=SFU_PAIR))["CRED_SOURCE"] == "sfu"


def test_a_partial_gateway_pair_refuses(tmp_path) -> None:
    """ARM THAT MUST GO RED. Half a credential cannot be completed from the other file
    without inventing a pair that authenticates nothing (round 2, Carnot)."""
    result = _run(tmp_path, gw={"LIVEKIT_API_KEY": "APIabc123"}, lk=SFU_PAIR)
    assert result.returncode == 1, "a half-present credential must refuse"
    assert "half-present" in result.stderr, result.stderr


def test_a_partial_sfu_pair_refuses(tmp_path) -> None:
    """ARM THAT MUST GO RED — the same corruption on the other file. A neighbour is
    not verified by its sibling."""
    result = _run(tmp_path, gw=PAIR, lk={"LIVEKIT_API_SECRET": "s3cret-aaa"})
    assert result.returncode == 1, "a half-present credential must refuse"
    assert "half-present" in result.stderr, result.stderr


def test_two_disagreeing_complete_pairs_refuse(tmp_path) -> None:
    """ARM THAT MUST GO RED, and the one with no safe guess. Either pair could be what
    the RUNNING SFU was started with; picking wrong mints tokens it rejects, with no
    error anywhere. Fail closed and make a human reconcile."""
    result = _run(tmp_path, gw=PAIR,
                  lk={"LIVEKIT_API_KEY_ID": "APIdifferent", "LIVEKIT_API_SECRET": "other"})
    assert result.returncode == 1, "two live-looking pairs must not be silently resolved"
    assert "DIFFERENT LiveKit key pairs" in result.stderr, result.stderr


def test_the_secret_is_never_printed(tmp_path) -> None:
    """The resolver reports the SOURCE, not the pair — so a credential never enters a
    pipe, a log, or a CI transcript. Pins the contract, not just today's behaviour."""
    result = _run(tmp_path, gw=PAIR, lk=SFU_PAIR)
    assert result.returncode == 0, result.stderr
    assert "s3cret-aaa" not in result.stdout
    assert "APIabc123" not in result.stdout
