"""Gateway operator-choice resolution (#3734) — the other half of PR#151's class.

PR#151 derived the invariant "an operator CHOICE must be recorded and read back; a
measured FACT should be re-derived" and closed it inside standup.sh's MEDIA wiring.
The class pass was scoped to one code path. Testing that claim found
GATEWAY_SEED_PEERS with the identical hole in the identical file, and a systematic
sweep of the .env heredoc found a SECOND instance, PASSKEY_ENABLED.

BOTH FAILURES ARE SILENT, which is why they need a suite that can go red:

  * GATEWAY_SEED_PEERS is the federation link. Each live island's seed list is
    literally the other island. Reset to [], peers_service serves self and stops —
    no fetch, no error, no log line. The islands quietly stop knowing each other.
  * PASSKEY_ENABLED is the PRIMARY sign-in ingress since social was dropped (#1923).
    Reset to false, /v1/auth/providers stops advertising passkeys and nobody can sign
    in, while the health check stays green.

The load-bearing tests are the two `recorded_*_beats_convention_without_the_flag`
cases. Each is PAIRED with a must-fail arm proving the assertion discriminates —
a check whose outcome is independent of the thing it checks is not a check.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "resolve-gateway-env.sh"

PEERS_A = '[{"id":"enspyr","display_name":"Enspyr","base_url":"https://chat.enspyr.co"}]'
PEERS_B = '[{"id":"imagineering","display_name":"Imagineering","base_url":"https://chat.imagineering.cc"}]'


def _write(path: Path, kv: dict[str, str]) -> Path:
    path.write_text("".join(f"{k}={v}\n" for k, v in kv.items()))
    return path


def _run(tmp_path, gw=None, seed_flag="", passkeys_flag=""):
    gw_file = _write(tmp_path / "gateway.env", gw) if gw is not None else tmp_path / "absent.env"
    return subprocess.run(
        [str(SCRIPT), str(gw_file), seed_flag, passkeys_flag],
        capture_output=True, text=True)


def _parsed(result) -> dict[str, str]:
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.strip().splitlines())


# --- seed peers: flag > recorded > convention -------------------------------

def test_fresh_island_is_solo(tmp_path) -> None:
    """No .env at all — a brand new island federates with nobody."""
    out = _parsed(_run(tmp_path))
    assert out["SEED_PEERS"] == "[]"


def test_seed_peers_flag_wins_over_the_record(tmp_path) -> None:
    out = _parsed(_run(tmp_path, gw={"GATEWAY_SEED_PEERS": PEERS_A}, seed_flag=PEERS_B))
    assert out["SEED_PEERS"] == PEERS_B


def test_recorded_seed_peers_beat_convention_without_the_flag(tmp_path) -> None:
    """THE #3734 REGRESSION. An operator stands up with --seed-peers, then re-runs
    later (say with --with-media) and does not repeat the flag. Before this fix
    SEED_PEERS reset to "[]" and the .env write then OVERWROTE the recorded value —
    on the invocation the header documents as safe to re-run.

    Worse than the hostname bug it rhymes with: a wrong hostname produces a CONNECT
    failure, whereas an emptied peer list produces a perfectly healthy island that
    has simply forgotten the network it belongs to."""
    out = _parsed(_run(tmp_path, gw={"GATEWAY_SEED_PEERS": PEERS_A}))
    assert out["SEED_PEERS"] == PEERS_A, "convention overwrote the recorded federation link"


def test_the_seed_peers_regression_test_can_actually_fail(tmp_path) -> None:
    """MUST-FAIL ARM for the test above. With nothing recorded the resolver returns
    the convention value, which is a DIFFERENT string from PEERS_A — so the assertion
    above genuinely discriminates rather than comparing convention to convention."""
    out = _parsed(_run(tmp_path))
    assert out["SEED_PEERS"] == "[]"
    assert out["SEED_PEERS"] != PEERS_A


def test_explicit_empty_seed_peers_flag_beats_a_recorded_list(tmp_path) -> None:
    """Going solo is a real choice and must be expressible. An explicit --seed-peers
    '[]' is an operator statement, not an absence, so it wins over the record."""
    out = _parsed(_run(tmp_path, gw={"GATEWAY_SEED_PEERS": PEERS_A}, seed_flag="[]"))
    assert out["SEED_PEERS"] == "[]"


# --- passkeys: the second instance, found by sweeping the class -------------

def test_fresh_island_does_not_advertise_passkeys(tmp_path) -> None:
    """Default off: advertising before /.well-known serves dies mid-ceremony."""
    out = _parsed(_run(tmp_path))
    assert out["PASSKEY_ENABLED"] == "false"


def test_recorded_passkeys_beat_convention_without_the_flag(tmp_path) -> None:
    """THE SECOND INSTANCE. Identical shape, and arguably worse than the seed-peer
    hole: passkeys are the primary ingress since social sign-in was dropped, so a
    re-run without --enable-passkeys stopped advertising them and locked users out of
    an island whose health check reported green."""
    out = _parsed(_run(tmp_path, gw={"PASSKEY_ENABLED": "true"}))
    assert out["PASSKEY_ENABLED"] == "true", "a re-run silently withdrew the sign-in method"


def test_the_passkeys_regression_test_can_actually_fail(tmp_path) -> None:
    """MUST-FAIL ARM for the test above — pins that the convention value differs from
    the recorded one, so the assertion can go red."""
    out = _parsed(_run(tmp_path))
    assert out["PASSKEY_ENABLED"] == "false"
    assert out["PASSKEY_ENABLED"] != "true"


def test_no_passkeys_flag_turns_them_off_against_the_record(tmp_path) -> None:
    """Read-back creates a new requirement: once "true" is recorded, absence of a flag
    preserves it, so there must be a way to say off. Without --no-passkeys the choice
    would be one-way and "false" would be ambiguous between "operator chose off" and
    "never set"."""
    out = _parsed(_run(tmp_path, gw={"PASSKEY_ENABLED": "true"}, passkeys_flag="false"))
    assert out["PASSKEY_ENABLED"] == "false"


def test_enable_passkeys_flag_wins_against_a_recorded_false(tmp_path) -> None:
    out = _parsed(_run(tmp_path, gw={"PASSKEY_ENABLED": "false"}, passkeys_flag="true"))
    assert out["PASSKEY_ENABLED"] == "true"


# --- refusals and parsing ---------------------------------------------------

def test_a_non_boolean_recorded_passkey_value_refuses(tmp_path) -> None:
    """ARM THAT MUST GO RED. A hand-edited .env holding PASSKEY_ENABLED=yes must not
    be silently coerced — config.py reads a strict boolean, and a value that parses
    as false-by-fallback would withdraw the sign-in method without saying so."""
    result = _run(tmp_path, gw={"PASSKEY_ENABLED": "yes"})
    assert result.returncode == 1, "a non-boolean passkey value must refuse"
    assert "true or false" in result.stderr


def test_the_last_assignment_wins_matching_python_dotenv(tmp_path) -> None:
    """A hand-edited .env can end up with a key twice. The resolver must agree with
    the parser the gateway actually uses, or the resolved value and the running value
    diverge — the disagreement being invisible."""
    env = tmp_path / "gateway.env"
    env.write_text(f"GATEWAY_SEED_PEERS={PEERS_A}\nGATEWAY_SEED_PEERS={PEERS_B}\n")
    result = subprocess.run([str(SCRIPT), str(env), "", ""], capture_output=True, text=True)
    assert _parsed(result)["SEED_PEERS"] == PEERS_B


def test_a_json_value_containing_equals_survives_intact(tmp_path) -> None:
    """The read splits on '=' and the payload is JSON that may legitimately contain
    one (a base_url query string). Truncating at the first '=' would produce
    malformed JSON that peers_service rejects — silently, back to serving self."""
    tricky = '[{"id":"x","display_name":"X","base_url":"https://x.example.org/?a=b"}]'
    out = _parsed(_run(tmp_path, gw={"GATEWAY_SEED_PEERS": tricky}))
    assert out["SEED_PEERS"] == tricky


def test_a_multiline_seed_peers_value_is_collapsed_to_one_line(tmp_path) -> None:
    """CARNOT'S BLOCKING FINDING (cage-match round 1). `docs/standup-guide.md` documents
    a MULTILINE `--seed-peers` array. The resolver emits a newline-delimited KEY=VALUE
    stream and the caller reads it line by line, so a newline-bearing value was read
    back as `SEED_PEERS=[` — silently replacing the federation list with malformed JSON
    that `peers_service` rejects, back to serving self. The documented invocation was
    the trigger.

    Newlines are insignificant whitespace outside a JSON string, so collapsing them is
    safe, and a line-oriented `.env` could never have carried a multiline value anyway.
    The guarantee lives in the resolver rather than in each caller — one door."""
    multiline = '[\n  {"id":"a","display_name":"A","base_url":"https://a.example.org"},\n  {"id":"b","display_name":"B","base_url":"https://b.example.org"}\n]'
    out = _parsed(_run(tmp_path, seed_flag=multiline))
    assert "\n" not in out["SEED_PEERS"], "a newline survived into the KEY=VALUE stream"
    assert out["SEED_PEERS"].startswith("[{") or out["SEED_PEERS"].startswith("[ ")
    assert '"id":"b"' in out["SEED_PEERS"], "the tail of the array was truncated away"


def test_the_multiline_test_can_actually_fail(tmp_path) -> None:
    """MUST-FAIL ARM for the test above. Pins that the input genuinely CONTAINED the
    newlines being collapsed — otherwise the assertion would pass against any value
    and could never go red."""
    multiline = '[\n  {"id":"a"}\n]'
    assert "\n" in multiline
    out = _parsed(_run(tmp_path, seed_flag=multiline))
    assert len(out["SEED_PEERS"].splitlines()) == 1


def test_a_stdout_only_protocol_never_emits_a_bare_newline(tmp_path) -> None:
    """The caller's line-oriented read is only safe if the resolver's contract holds for
    BOTH keys. Assert the whole stream is exactly two lines regardless of input shape,
    so a future key added without normalisation trips this rather than the federation
    link."""
    result = _run(tmp_path, seed_flag='[\n{"id":"x"}\n]', passkeys_flag="true")
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.strip().splitlines()) == 2


def test_a_quoted_recorded_value_matches_python_dotenv(tmp_path) -> None:
    """CARNOT'S ROUND-2 FINDING. `PASSKEY_ENABLED="true"` is a valid .env line —
    python-dotenv strips the quotes and reads `true`. Before the strip, this resolver
    read the literal `"true"`, failed its own boolean check, and ABORTED standup on a
    file the guide explicitly invites operators to hand-edit. Fail-closed, so loud
    rather than silent, but a hard stop for no good reason.

    This is #3592's class (shell re-implementing python-dotenv) and this resolver is
    the third instance — so the parity that IS claimed gets a test."""
    for quoted in ('"true"', "'true'"):
        out = _parsed(_run(tmp_path, gw={"PASSKEY_ENABLED": quoted}))
        assert out["PASSKEY_ENABLED"] == "true", f"{quoted} did not match python-dotenv"


def test_the_quote_test_can_actually_fail(tmp_path) -> None:
    """MUST-FAIL ARM. Pins that an unquoted value and a quoted one are genuinely
    different input strings, so the assertion above discriminates."""
    assert '"true"' != "true"
    out = _parsed(_run(tmp_path, gw={"PASSKEY_ENABLED": '"false"'}))
    assert out["PASSKEY_ENABLED"] == "false"


def test_quotes_are_stripped_from_seed_peers_too(tmp_path) -> None:
    """The strip is in the shared reader, so it must hold for BOTH keys — otherwise a
    quoted peer list would round-trip its quotes into .env and back out again, growing
    a pair each run."""
    out = _parsed(_run(tmp_path, gw={"GATEWAY_SEED_PEERS": f'"{PEERS_A}"'}))
    assert out["SEED_PEERS"] == PEERS_A


def test_an_unbalanced_quote_is_left_alone(tmp_path) -> None:
    """Only a MATCHING pair is stripped. A value that merely starts with a quote is not
    a quoted value, and eating one end would corrupt it."""
    out = _parsed(_run(tmp_path, gw={"GATEWAY_SEED_PEERS": '"unbalanced'}))
    assert out["SEED_PEERS"] == '"unbalanced'
