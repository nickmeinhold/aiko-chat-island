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
