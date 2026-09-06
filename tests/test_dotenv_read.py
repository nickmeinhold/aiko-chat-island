"""The ONE .env reader shared by every deploy script (`deploy/lib/dotenv-read.sh`).

Four copies of `grep -E "^KEY=" | cut -d= -f2-` existed across deploy/, and every one
treated **a matcher miss as "never recorded"** — so any dotenv spelling the regex did not
cover resolved to the convention default, silently, on a re-run the script advertises as
safe. Cage-match round 4 (Tesla) named the structural form: *non-match is not absence.*

The worst instance was not in the script this PR was written for. `standup.sh` read the
existing JWT secret with `^JWT_SECRET=`; an `export JWT_SECRET=…` or a leading space read
as nothing, so standup MINTED A NEW ONE — invalidating every live session on the island,
under a header that promises it never rotates a secret. That is the test at the bottom of
this file, and it is the reason the grammar lives in one sourced file rather than in four
greps that have to be remembered together.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "deploy" / "lib" / "dotenv-read.sh"


def _read(tmp_path: Path, body: str, key: str = "PASSKEY_ENABLED") -> str:
    env = tmp_path / ".env"
    env.write_text(body)
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"; dotenv_read "{env}" {key}'],
        capture_output=True, text=True, check=True).stdout


# --- the grammar: [ws] [export ws] KEY [ws] = [ws] VALUE [ws] [CR] ----------

def test_every_grammar_element_is_tolerated(tmp_path) -> None:
    for body in (
        "PASSKEY_ENABLED=true\n",
        "  PASSKEY_ENABLED=true\n",
        "export PASSKEY_ENABLED=true\n",
        "export   PASSKEY_ENABLED=true\n",
        "PASSKEY_ENABLED =true\n",
        "PASSKEY_ENABLED= true\n",
        "PASSKEY_ENABLED = true\n",
        "PASSKEY_ENABLED=true \n",
        "PASSKEY_ENABLED=true\r\n",
        'PASSKEY_ENABLED="true"\n',
        "PASSKEY_ENABLED='true'\n",
        '  export PASSKEY_ENABLED = "true"  \r\n',
    ):
        assert _read(tmp_path, body) == "true", f"grammar rejected {body!r}"


def test_absence_is_still_absence(tmp_path) -> None:
    """MUST-FAIL ARM for the grammar: a tolerant matcher that matches ANYTHING would make
    every test above pass vacuously. A genuinely absent key must still read empty."""
    assert _read(tmp_path, "SOMETHING_ELSE=true\n") == ""
    assert _read(tmp_path, "") == ""
    assert _read(tmp_path, "# PASSKEY_ENABLED=true\n") == "", "a commented-out line is not a value"


def test_a_similarly_named_key_is_not_matched(tmp_path) -> None:
    """The relaxed grammar must not have relaxed the KEY boundary too — `PASSKEY_ENABLED`
    must not be satisfied by `NOT_PASSKEY_ENABLED` or `PASSKEY_ENABLED_EXTRA`."""
    assert _read(tmp_path, "PASSKEY_ENABLED_EXTRA=true\n") == ""


def test_last_assignment_wins(tmp_path) -> None:
    assert _read(tmp_path, "PASSKEY_ENABLED=false\nexport PASSKEY_ENABLED = true\n") == "true"


# --- the severe one --------------------------------------------------------

SECRET = "a" * 64


def test_an_export_prefixed_jwt_secret_is_found_and_not_re_minted(tmp_path) -> None:
    """THE ROUND-4 SEVERE FINDING (Tesla). `standup.sh` preserves the JWT secret across
    re-runs so live sessions survive — its header promises it "never rotates an existing
    JWT secret". It found that secret with `^JWT_SECRET=`, so an `export JWT_SECRET=…`
    line read as ABSENT and standup minted a fresh one, silently invalidating every
    session on the island.

    It was listed in this PR's own enumeration table as "already read back" — a claim
    inherited from a code comment rather than a measurement. Tesla: "the table's JWT row
    is a claim, not a measurement."
    """
    for body in (
        f"export JWT_SECRET={SECRET}\n",
        f"  JWT_SECRET={SECRET}\n",
        f"JWT_SECRET = {SECRET}\n",
        f'JWT_SECRET="{SECRET}"\n',
        f"JWT_SECRET={SECRET}\r\n",
    ):
        got = _read(tmp_path, body, key="JWT_SECRET")
        assert got == SECRET, f"secret not recovered from {body!r} — standup would re-mint"


def test_the_jwt_test_can_actually_fail(tmp_path) -> None:
    """MUST-FAIL ARM. standup mints a new secret when the read returns empty OR shorter
    than 32 chars, so pin that an absent secret genuinely reads empty — otherwise the
    assertions above could never distinguish found from re-minted."""
    assert _read(tmp_path, "OTHER=x\n", key="JWT_SECRET") == ""
    assert len(SECRET) >= 32, "the fixture must clear standup's 32-char floor"


def test_an_unreadable_file_reads_as_empty_which_is_why_callers_must_check(tmp_path) -> None:
    """CARNOT'S ROUND-5 FINDING, pinned as a CONTRACT rather than papered over.

    `dotenv_read` cannot distinguish "key absent" from "file unreadable" — both give "".
    Carnot found the consequence: standup's JWT path treated that as "first run" and MINTED
    A NEW SECRET on a permissions error, logging out every user. Same non-match-is-absence
    shape Tesla named in round 4, one layer up.

    The reader deliberately stays pure — only the caller knows whether absence is benign (a
    brand-new island legitimately has no JWT_SECRET; an unreadable .env never is). So
    standup.sh now refuses on `[ -r "$ENV_FILE" ]` before treating absence as a fresh
    install. This test pins the ambiguity so nobody later 'fixes' the reader and leaves the
    caller's guard looking redundant."""
    env = tmp_path / ".env"
    env.write_text("JWT_SECRET=" + "a" * 64 + "\n")
    env.chmod(0o000)
    try:
        got = subprocess.run(
            ["bash", "-c", f'. "{LIB}"; dotenv_read "{env}" JWT_SECRET'],
            capture_output=True, text=True, check=True).stdout
    finally:
        env.chmod(0o600)
    assert got == "", "if this now reports an error, standup's -r guard can be simplified"


# --- the guarantee is the FUNCTION's, not the caller's (#3948) ---------------
#
# `dotenv_keys` shells out through several tools. A pipeline reports only its LAST
# command's status unless `set -o pipefail` is in force, so a tool sitting MID-PIPE can
# die and the pipeline still exits 0 — with an empty key list. The caller then computes
# "nothing would be dropped" and replaces .env, destroying every key it could not see.
#
# Every caller happens to set `pipefail`. That was the defect, not the mitigation: a
# sourced library stating a data-loss guarantee cannot rest it on a shell option set in a
# different file, which it neither sets nor asserts and cannot see. These tests run WITHOUT
# pipefail on purpose — under the old implementation the `sed` and `tr` arms returned
# 0 with no keys, which is the exact success-shaped failure that costs a signing seed.
#
# The claim that used to sit in standup.sh — that a missing `sort` "fails OPEN" — is
# measured false here: `sort` is terminal, so it always propagated. The prose named the
# one tool that could not do the thing it warned about.

def _fake_bin(tmp_path: Path, omit: str) -> Path:
    """A PATH containing every tool dotenv_keys uses except `omit`."""
    import shutil
    bindir = tmp_path / f"bin-no-{omit}"
    bindir.mkdir()
    for tool in ("grep", "tr", "sed", "sort", "cat"):
        if tool == omit:
            continue
        real = shutil.which(tool, path="/usr/bin:/bin:/usr/local/bin")
        assert real, f"cannot build the probe: {tool} not found on this machine"
        (bindir / tool).symlink_to(real)
    assert not (bindir / omit).exists()
    return bindir


def _keys_without_pipefail(env: Path, path: str | None = None):
    """Run dotenv_keys with pipefail explicitly OFF. Returns (rc, keys).

    bash is invoked by ABSOLUTE path: the point of the probe is to control what the
    function can find on PATH, and a relative "bash" would itself be unfindable under a
    PATH that deliberately contains only the tools under test. (First draft did exactly
    that and failed with FileNotFoundError — the instrument breaking, not the code.)
    """
    import shutil
    bash = shutil.which("bash") or "/bin/bash"
    env_vars = {"PATH": path} if path else None
    r = subprocess.run(
        [bash, "-c", f'set +o pipefail; . "{LIB}"; dotenv_keys "{env}"'],
        capture_output=True, text=True, env=env_vars)
    return r.returncode, r.stdout.split()


def test_positive_control_the_probe_can_read_keys(tmp_path) -> None:
    """Without this arm, every test below passes on a probe that never worked."""
    env = tmp_path / ".env"
    env.write_text("ALPHA=1\nBETA=2\nGAMMA=3\n")
    rc, keys = _keys_without_pipefail(env)
    assert rc == 0
    assert keys == ["ALPHA", "BETA", "GAMMA"]


def test_a_missing_mid_pipe_tool_never_reads_as_an_empty_file(tmp_path) -> None:
    """THE MUST-FAIL ARM. Under the pre-#3948 implementation these returned (0, []).

    Zero keys with a SUCCESS status is indistinguishable from "this file assigns
    nothing", and that is precisely what the key-loss guard consumes.
    """
    env = tmp_path / ".env"
    env.write_text("ALPHA=1\nBETA=2\nISLAND_SIGNING_SEED=irreplaceable\n")

    for omitted in ("sed", "tr"):
        bindir = _fake_bin(tmp_path, omitted)
        rc, keys = _keys_without_pipefail(env, path=str(bindir))
        assert rc != 0, (
            f"dotenv_keys returned SUCCESS with {omitted} missing and pipefail off — "
            "the caller would read this as 'no keys' and replace .env"
        )
        assert keys == []


def test_sort_was_never_the_fail_open_the_comment_claimed(tmp_path) -> None:
    """`sort` is terminal, so it propagated even before the fix — kept as the control
    that distinguishes 'we fixed the real hole' from 'everything fails closed now'."""
    env = tmp_path / ".env"
    env.write_text("ALPHA=1\n")
    rc, keys = _keys_without_pipefail(env, path=str(_fake_bin(tmp_path, "sort")))
    assert rc != 0
    assert keys == []
