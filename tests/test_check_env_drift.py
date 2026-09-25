"""Tests for deploy/check-env-drift.py.

The ssh and sops halves are I/O and are exercised by running the thing. What is
tested here is the part where a bug would be SILENT: the comparison, the
wrong-box guard, and the refusal to call an empty parse a match.

Note what each test CONSTRUCTS. A test asserting "no differences found" passes
just as well when the comparison never ran — so every negative case here is
paired with a positive control that must fail for the opposite reason.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "deploy" / "check-env-drift.py"
_spec = importlib.util.spec_from_file_location("check_env_drift", _SRC)
ced = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ced)


# --- the comparison ---------------------------------------------------------

def test_identical_envs_report_no_difference():
    env = {"A": "1", "B": "2"}
    assert ced.compare(dict(env), dict(env)) == ([], [], [])


def test_a_changed_value_is_reported_and_is_not_confused_with_a_missing_key():
    """Positive control for the test above: same key sets, one value differs."""
    only_repo, only_live, changed = ced.compare({"A": "1"}, {"A": "2"})
    assert (only_repo, only_live) == ([], [])
    assert changed == ["A"]


def test_key_only_in_repo_and_key_only_on_box_land_in_different_buckets():
    only_repo, only_live, changed = ced.compare({"A": "1", "R": "x"}, {"A": "1", "L": "y"})
    assert only_repo == ["R"]
    assert only_live == ["L"]
    assert changed == []


def test_the_2026_09_11_shape_is_visible():
    """The incident: a key present in .env that the other half never learned about.

    This is the case the tool exists for, so it gets its own name.
    """
    only_repo, only_live, changed = ced.compare(
        {"APNS_VOIP_TOPIC": "co.enspyr.chat.voip"}, {}
    )
    assert only_repo == ["APNS_VOIP_TOPIC"]


# --- the parser -------------------------------------------------------------

def test_a_pem_with_real_newlines_inside_quotes_parses_as_one_value():
    """APNS_PRIVATE_KEY is why these files are SOPS *binary* and not *dotenv*.

    A parser that splits on newlines would shred this into junk keys and report
    catastrophic drift on every run.
    """
    text = 'A=1\nAPNS_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nMIGT\nAgEA\n-----END PRIVATE KEY-----"\nB=2\n'
    parsed = ced.parse_dotenv(text, "test")
    assert set(parsed) == {"A", "APNS_PRIVATE_KEY", "B"}
    assert "BEGIN PRIVATE KEY" in parsed["APNS_PRIVATE_KEY"]
    assert "END PRIVATE KEY" in parsed["APNS_PRIVATE_KEY"]


def test_an_empty_parse_refuses_rather_than_reporting_a_match():
    """A file that read as empty must never compare equal to another empty read.

    Zero keys on both sides is the shape of "the instrument did not run", and it
    would otherwise present as the cleanest possible MATCH.
    """
    with pytest.raises(ced.CannotRun):
        ced.parse_dotenv("", "test")

    with pytest.raises(ced.CannotRun):
        ced.parse_dotenv("# only a comment\n\n", "test")


# --- the wrong-box guard ----------------------------------------------------

def test_matching_island_identity_is_allowed_through():
    repo = {"ISLAND_ID": "enspyr", "ISLAND_DISPLAY_NAME": "Enspyr"}
    ced.assert_same_island("enspyr", repo, dict(repo))  # must not raise


def test_a_wrong_box_refuses_instead_of_reporting_a_wall_of_drift():
    """Both boxes are shared hosts. Comparing against the wrong one would print
    a catastrophic-looking diff that is entirely an artifact of the mistake."""
    with pytest.raises(ced.CannotRun, match="WRONG BOX"):
        ced.assert_same_island(
            "enspyr",
            {"ISLAND_ID": "enspyr"},
            {"ISLAND_ID": "imagineering"},
        )


def test_a_missing_identity_key_does_not_fabricate_a_refusal():
    """Positive control for the guard: absence is not a mismatch.

    An older box may predate ISLAND_DISPLAY_NAME; that is not evidence of the
    wrong box, and treating it as such would block a legitimate check.
    """
    ced.assert_same_island("enspyr", {"ISLAND_ID": "enspyr"}, {})  # must not raise


# --- the island table -------------------------------------------------------

def test_every_known_island_has_an_encrypted_config_committed():
    for island in ced.ISLANDS:
        assert (ced.REPO / "deploy" / "secrets" / f"{island}.env.sops").is_file(), island


def test_exit_codes_are_distinct_and_cannot_run_is_not_success():
    assert ced.EXIT_MATCH == 0
    assert len({ced.EXIT_MATCH, ced.EXIT_DIFFERS, ced.EXIT_CANNOT_RUN}) == 3
    assert ced.EXIT_CANNOT_RUN != ced.EXIT_MATCH
