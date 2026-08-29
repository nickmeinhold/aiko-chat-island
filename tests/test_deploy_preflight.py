"""The APNs half-configured preflight (claude-tasks#3366).

Forwarding APNS_* made previously-dead bytes reach the container, where the
half-configured guard refuses to boot. With `restart: always` that is a crash-loop
on a box nobody edited, triggered by a version bump — so `deploy/update.sh` checks
the box's .env BEFORE the backup, while the island is still running.

BOTH CONTROLS ARE BUILT HERE, not just the failing case: an arm that must go red
(partial) and arms that must stay green (all four, none, blank-valued). A preflight
that aborted on a healthy box would be worse than no preflight — it would block
every deploy of an island that has push switched off, which is most of them.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "preflight-apns.sh"

ALL_FOUR = {
    "APNS_KEY_ID": "ABC123DEFG",
    "APNS_TEAM_ID": "TEAM123456",
    "APNS_TOPIC": "cc.example.testapp",
    "APNS_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----\\nxx\\n-----END PRIVATE KEY-----",
}


def _run(tmp_path: Path, lines: list[str]) -> subprocess.CompletedProcess:
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines) + "\n")
    return subprocess.run([str(SCRIPT), str(env_file)], capture_output=True, text=True)


def test_no_apns_keys_at_all_is_fine(tmp_path) -> None:
    """The common case by far. Push off is an HONEST state — absent credentials
    mean is_configured() is False and the operator knows. Must not block a deploy."""
    result = _run(tmp_path, ["ENVIRONMENT=production", "ISLAND_VERSION=0.9.1"])
    assert result.returncode == 0, result.stderr


def test_all_four_present_is_fine(tmp_path) -> None:
    """A fully-configured island. Must not block a deploy either — this is what
    imagineering looks like."""
    result = _run(tmp_path, [f"{k}={v}" for k, v in ALL_FOUR.items()])
    assert result.returncode == 0, result.stderr


def test_a_partial_set_aborts_and_names_both_sides(tmp_path) -> None:
    """THE ARM THAT MUST GO RED — the half-drafted credential set the issue is about.

    The message has to name which keys are set AND which are missing: an operator
    reading 'APNs is half-configured' with no list still has to go and diff it, and
    this fires at the moment they are trying to ship something else."""
    partial = {k: v for k, v in ALL_FOUR.items() if k != "APNS_PRIVATE_KEY"}
    result = _run(tmp_path, [f"{k}={v}" for k, v in partial.items()])
    assert result.returncode == 1, "a partial set must abort the deploy"
    assert "APNS_PRIVATE_KEY" in result.stderr, result.stderr
    assert "APNS_KEY_ID" in result.stderr, result.stderr


def test_a_blank_value_counts_as_UNSET_not_as_partial(tmp_path) -> None:
    """The interaction with claude-tasks#3358, and the arm most likely to be got wrong.

    config.py restores absence for a whitespace-only value, so `APNS_TOPIC="   "` is
    UNSET to the island and the box boots fine. If this preflight counted a blank as
    'present', it would abort a deploy that would have succeeded — a false red on a
    healthy box, which is the failure mode that gets a safety check deleted."""
    lines = [f"{k}={v}" for k, v in ALL_FOUR.items() if k != "APNS_TOPIC"]
    lines.append("APNS_TOPIC=   ")
    result = _run(tmp_path, lines)
    assert result.returncode == 1, (
        "three real keys plus one BLANK is still a partial set — the blank does not "
        "complete it"
    )
    assert "APNS_TOPIC" in result.stderr, result.stderr


def test_all_blank_is_the_same_as_absent(tmp_path) -> None:
    """Every key present but blank is 'the operator said nothing' four times over —
    is_configured() is False and the island boots. Must not abort."""
    result = _run(tmp_path, [f"{k}=" for k in ALL_FOUR])
    assert result.returncode == 0, result.stderr


def test_a_missing_env_file_is_not_an_error(tmp_path) -> None:
    """A first standup has no .env yet; that is standup.sh's business, not this
    check's. Silence here, not a spurious abort."""
    result = subprocess.run(
        [str(SCRIPT), str(tmp_path / "nope.env")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
