"""`deploy/standup.sh` end to end, against the real script (#3761).

WHY THIS EXISTS, and the claim it retires. Every prior round of PR#153's cage-match said
standup.sh "cannot be tested — it needs a live Docker daemon and has no dry-run", and that
sentence was inherited from a comment in PR#151 and repeated three times without anyone
measuring it. Nick asked "why isn't it testable?" and the answer turned out to be: it is.

Every docker call before the `.env` is written is `docker compose version`, `docker info`,
`docker volume inspect` and `docker volume create` — all PATH-stubbable. The media and TLS
blocks are behind flags. So the destructive path CAN be driven, and this drives it.

That matters because the untested half is where the consequences live: it decides whether
to preserve the federation link, whether to keep advertising passkeys, and whether to mint
a NEW JWT secret — which logs out every user on the island. Those decisions had no test.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from dataclasses import dataclass
from typing import Callable
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parents[1]
FIXED_SECRET = "f" * 64
PEERS = '[{"id":"enspyr","display_name":"Enspyr","base_url":"https://chat.enspyr.co"}]'


@dataclass
class Island:
    root: Path
    env_file: Path
    calls: Path
    run: Callable[..., subprocess.CompletedProcess]


def _stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(0o755)


@pytest.fixture
def island(tmp_path):
    """A copy of the deploy tree plus stubbed docker/openssl/curl.

    A COPY, not the real repo: standup.sh writes `$REPO_ROOT/.env`, and pointing it at the
    checkout would clobber a developer's own file. REPO_ROOT is derived from the script's
    location, so a copied tree relocates cleanly with no production flag added for
    testability."""
    root = tmp_path / "island"
    (root).mkdir()
    shutil.copytree(REPO / "deploy", root / "deploy", symlinks=True)
    for f in ("docker-compose.yml", "docker-compose.build.yml"):
        if (REPO / f).exists():
            shutil.copy(REPO / f, root / f)

    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "calls.log"
    # Honest stubs: `volume inspect` FAILS so the create path runs, everything else
    # succeeds, and every invocation is recorded so a test can assert what was called.
    _stub(bin_dir, "docker", f'''
echo "docker $*" >> "{log}"
case "$1 $2" in
  "volume inspect") exit 1 ;;
esac
exit 0
''')
    _stub(bin_dir, "openssl", f'echo "openssl $*" >> "{log}"\necho {FIXED_SECRET}\n')
    # /health answers immediately so the 30x2s wait loop does not run.
    _stub(bin_dir, "curl", f'echo "curl $*" >> "{log}"\nexit 0\n')

    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")

    def run(*args, expect_ok=True):
        r = subprocess.run(
            [str(root / "deploy" / "standup.sh"), "--domain", "chat.example.org",
             "--name", "Example Island", "--no-tls", "--yes", *args],
            capture_output=True, text=True, env=env, cwd=str(root), timeout=120)
        if expect_ok:
            assert r.returncode == 0, f"standup failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}"
        return r

    return Island(root=root, env_file=root / ".env", calls=log, run=run)


def _values(island) -> dict:
    return dict(dotenv_values(str(island.env_file)))


# --- the harness must be able to fail -------------------------------------

def test_the_harness_actually_runs_the_real_script(island) -> None:
    """MUST-FAIL ARM for everything below. If the stubs short-circuited the script, or it
    died before writing anything, every assertion in this file would pass vacuously
    against a missing file. Pin that the real script ran and produced a real .env."""
    island.run()
    assert island.env_file.exists(), "standup.sh wrote no .env — the rest of this file is void"
    assert "docker compose up -d" in island.calls.read_text(), "the script never reached compose up"
    assert oct(island.env_file.stat().st_mode)[-3:] == "600", ".env must be owner-only"


def test_a_fresh_island_is_solo_and_quiet(island) -> None:
    island.run()
    v = _values(island)
    assert v["ISLAND_SEED_PEERS"] == "[]"
    assert v["PASSKEY_ENABLED"] == "false"
    assert v["JWT_SECRET"] == FIXED_SECRET
    assert "ENVIRONMENT" not in v, "absence of ENVIRONMENT is what arms the fail-closed JWT guard"


# --- #3734 itself, against the script that had the bug --------------------

def test_a_rerun_without_flags_preserves_both_operator_choices(island) -> None:
    """THE BUG THIS PR EXISTS FOR, asserted at last against the destructive script rather
    than against the resolver it calls.

    An operator stands up with peers and passkeys, then re-runs later — the guide's own
    documented invocation for enabling passkeys does not repeat --seed-peers. Before the
    fix, both silently reverted: the federation link emptied and the sign-in method
    withdrawn, on an island whose health check stayed green."""
    island.run("--seed-peers", PEERS, "--enable-passkeys")
    assert _values(island)["ISLAND_SEED_PEERS"] == PEERS

    island.run()          # <- no flags at all, the documented re-run
    v = _values(island)
    assert v["ISLAND_SEED_PEERS"] == PEERS, "the federation link was silently emptied"
    assert v["PASSKEY_ENABLED"] == "true", "passkey sign-in was silently withdrawn"


def test_the_rerun_test_can_actually_fail(island) -> None:
    """MUST-FAIL ARM. Pins that the convention default differs from the recorded value, so
    the assertions above discriminate rather than comparing a default to itself."""
    island.run()
    assert _values(island)["ISLAND_SEED_PEERS"] == "[]" != PEERS


def test_an_explicit_flag_still_changes_a_recorded_choice(island) -> None:
    """Preservation must not become a trap: a flag is how you CHANGE one."""
    island.run("--seed-peers", PEERS, "--enable-passkeys")
    island.run("--seed-peers", "[]", "--no-passkeys")
    v = _values(island)
    assert v["ISLAND_SEED_PEERS"] == "[]"
    assert v["PASSKEY_ENABLED"] == "false"


# --- the secret ------------------------------------------------------------

def test_a_rerun_never_rotates_the_jwt_secret(island) -> None:
    island.run()
    first = _values(island)["JWT_SECRET"]
    island.env_file.write_text(island.env_file.read_text().replace(FIXED_SECRET, "d" * 64))
    island.run()
    assert _values(island)["JWT_SECRET"] == "d" * 64, "a live secret was rotated on a re-run"


def test_an_export_prefixed_secret_is_not_re_minted(island) -> None:
    """CAGE-MATCH ROUND 4 (Tesla), now asserted at the real caller. `^JWT_SECRET=` missed
    an `export` prefix, read as absent, and minted a fresh secret — invalidating every
    session under a header promising it never rotates one."""
    island.run()
    hand_edited = "export JWT_SECRET=" + "e" * 64 + "\n"
    island.env_file.write_text(hand_edited)
    island.run()
    assert _values(island)["JWT_SECRET"] == "e" * 64, "standup re-minted over an export-prefixed secret"


def test_an_unreadable_env_aborts_instead_of_minting(island) -> None:
    """CAGE-MATCH ROUND 5 (Carnot). dotenv_read cannot tell "key absent" from "file
    unreadable"; both are empty, and standup treated that as a fresh island and minted.
    Mutation-testing showed deleting this guard reddened NOTHING — it is why #3761 stopped
    being a footnote."""
    island.run()
    island.env_file.chmod(0o000)
    try:
        r = island.run(expect_ok=False)
        assert r.returncode != 0, "an unreadable .env must abort, not mint a new secret"
        assert "not readable" in (r.stdout + r.stderr)
    finally:
        island.env_file.chmod(0o600)


# --- what the GATEWAY reads, which is the only layer that counts ----------

PEERS_HASH = '[{"id":"a","display_name":"Island #1","base_url":"https://a.example.org"}]'


def test_a_peer_name_containing_a_hash_survives_into_the_gateways_parser(island) -> None:
    """TESLA, CAGE-MATCH ROUND 5. python-dotenv ends an unquoted value at whitespace-then-
    `#`, so a perfectly legal peer name — "Island #1" — is truncated when the GATEWAY reads
    the file standup wrote. The array dies mid-string, peers_service can't parse it, and
    the island serves self: #3734 again, silently, with the health check green.

    The shell reader deliberately does NOT strip comments, so on the next re-run it reads
    the whole line back, the bracket check sees `[`…`]`, and standup rewrites the poison as
    though it were a deliberate choice.

    Every earlier guard missed it because they all tested the RESOLVER. The resolver is
    fine. The defect is in what the heredoc WRITES — which is only visible from here, and
    only by asking python-dotenv rather than the shell."""
    island.run("--seed-peers", PEERS_HASH)
    got = _values(island)["ISLAND_SEED_PEERS"]
    assert got == PEERS_HASH, (
        "the gateway will read a truncated peer list.\n"
        f"  wrote:      {PEERS_HASH}\n"
        f"  gateway reads: {got}"
    )


def test_the_hash_test_can_actually_fail(island) -> None:
    """MUST-FAIL ARM: pin that the fixture genuinely contains the ' #' sequence, so the
    assertion above is exercising the documented truncation and not passing by accident."""
    assert " #" in PEERS_HASH
    island.run("--seed-peers", PEERS)
    assert _values(island)["ISLAND_SEED_PEERS"] == PEERS, "the no-hash control must pass"


def test_a_display_name_containing_a_hash_survives_too(island) -> None:
    """The SECOND instance of Tesla's finding, and the worse one: --name is REQUIRED on
    every standup, so an operator naming their island "Island #1" had the gateway read
    "Island". Found by asking whether the hash hole was a class rather than fixing the one
    value that was reported."""
    island.run("--name", "Island #1")
    assert _values(island)["ISLAND_DISPLAY_NAME"] == "Island #1"


def test_quoted_values_round_trip_across_a_rerun(island) -> None:
    """Quote-on-write must not break READ-BACK: the reader strips one matching pair, so a
    value that is written quoted and read back must survive rather than accumulate or lose
    quotes. Two re-runs, because a single one cannot show accumulation.

    Only ISLAND_SEED_PEERS is asserted across re-runs. ISLAND_DISPLAY_NAME is quoted the
    same way but is NOT read back — `--name` is required on every invocation, so it is
    re-supplied rather than preserved, and it cannot accumulate. Its quoting is covered by
    the hash test above."""
    island.run("--name", "Island #1", "--seed-peers", PEERS_HASH)
    island.run()
    island.run()
    assert _values(island)["ISLAND_SEED_PEERS"] == PEERS_HASH, (
        "quotes accumulated or were lost across re-runs"
    )


def test_an_apostrophe_is_refused_loudly_with_the_escape_named(island) -> None:
    """The one shape single quotes cannot carry. Refused rather than silently mangled —
    and the message must say how to express it, or the operator is stuck."""
    r = island.run("--name", "Nick's Island", expect_ok=False)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    assert "single quote" in out and "u0027" in out, "the refusal must name the escape"
    assert not island.env_file.exists(), "nothing may be written when the value is refused"


def test_the_quoting_tests_can_actually_fail(island) -> None:
    """MUST-FAIL ARM for the pair above: pin that an UNQUOTED write is genuinely what
    python-dotenv truncates, so the assertions are exercising the real mechanism rather
    than passing because the fixture happens to be tame."""
    (island.root / "raw.env").write_text("K=Island #1\n")
    assert dotenv_values(str(island.root / "raw.env"))["K"] == "Island", (
        "python-dotenv stopped truncating at ' #' — the quote-on-write guard may now be "
        "unnecessary; re-verify before removing it"
    )


# --- the retired spelling, through the REAL script ---------------------------
#
# The resolver's own suite proves the refusal; these prove what standup DOES with it.
# A stop that fires after the .env has already been rewritten would destroy the very
# federation list it exists to protect, so "refuses" is only half the property —
# "refuses WITHOUT touching the file" is the half that makes it safe.

def test_a_legacy_only_seed_list_makes_standup_refuse(island) -> None:
    island.run("--seed-peers", PEERS)          # a real, populated .env
    island.env_file.write_text(
        island.env_file.read_text().replace("ISLAND_SEED_PEERS=", "GATEWAY_SEED_PEERS=")
    )
    result = island.run(expect_ok=False)       # the documented unflagged re-run
    assert result.returncode != 0, (
        "standup completed against a legacy-only seed list — it would have written the "
        "[] convention back and unpeered this island"
    )
    assert "ISLAND_SEED_PEERS" in (result.stdout + result.stderr), (
        "the resolver's actionable message must survive out through standup; an operator "
        "who cannot see WHICH key to rename cannot act on the refusal"
    )


def test_the_refusal_does_not_overwrite_the_existing_env(island) -> None:
    """The half that makes the stop safe rather than merely loud. standup resolves BEFORE
    it rewrites (deploy/standup.sh: resolver, then the mktemp+mv heredoc), so an aborted
    run must leave the operator's file byte-identical — their seed list is still there to
    be renamed by hand."""
    island.run("--seed-peers", PEERS)
    island.env_file.write_text(
        island.env_file.read_text().replace("ISLAND_SEED_PEERS=", "GATEWAY_SEED_PEERS=")
    )
    before = island.env_file.read_bytes()
    island.run(expect_ok=False)
    assert island.env_file.read_bytes() == before, (
        "the aborted run mutated .env — the refusal destroyed the seed list it exists to save"
    )
    assert PEERS.encode() in before, "fixture void: the preserved file must contain the peers"


def test_the_standup_refusal_arm_can_actually_pass(island) -> None:
    """NULL ARM. The same re-run against a CANONICAL .env must SUCCEED and preserve the
    list — without this, a standup.sh that refused every re-run would satisfy both tests
    above."""
    island.run("--seed-peers", PEERS)
    island.run()
    assert _values(island)["ISLAND_SEED_PEERS"] == PEERS


# --- the whole-file rewrite must not destroy what it does not write (#3921) ---
#
# Measured against the two live islands: a documented unflagged re-run would have taken
# 15 keys off imagineering and 11 off enspyr, including ISLAND_SIGNING_SEED and
# APNS_PRIVATE_KEY (which exist nowhere else) and ISLAND_VERSION (whose loss silently
# unpins the next deploy). Each test below runs a CLEAN re-run first, so a guard that
# simply refused everything would fail its own first assertion.

def test_a_rerun_refuses_to_destroy_a_key_it_does_not_write(island) -> None:
    island.run()
    island.run()  # the null arm, inline: an unadorned re-run must still SUCCEED

    before = island.env_file.read_text() + "ISLAND_SIGNING_SEED=not-recoverable-anywhere\n"
    island.env_file.write_text(before)

    result = island.run(expect_ok=False)
    assert result.returncode != 0, (
        "standup rewrote a .env holding a key it does not write — the signing seed is gone "
        "and exists nowhere else"
    )
    assert "ISLAND_SIGNING_SEED" in (result.stdout + result.stderr), (
        "the refusal must NAME the keys at risk; 'refusing to rewrite' alone tells an "
        "operator nothing about what they nearly lost"
    )
    assert island.env_file.read_text() == before, (
        "the aborted run mutated .env — the guard destroyed what it exists to protect"
    )


def test_the_key_loss_guard_uses_the_reader_grammar_not_a_naive_match(island) -> None:
    """An `export`-prefixed key must be protected too.

    This is not a hypothetical shape: an `export JWT_SECRET=` once read as absent and
    standup MINTED A NEW SECRET, logging out every user (see deploy/lib/dotenv-read.sh's
    header). A guard using `^[A-Z_]+=` would report that key as safe to destroy — so the
    lister shares the reader's grammar rather than approximating it."""
    island.run()
    before = island.env_file.read_text() + "export APNS_PRIVATE_KEY=-----BEGIN-FAKE-----\n"
    island.env_file.write_text(before)

    result = island.run(expect_ok=False)
    assert result.returncode != 0, "an export-prefixed key was not seen and would be destroyed"
    assert "APNS_PRIVATE_KEY" in (result.stdout + result.stderr)
    assert island.env_file.read_text() == before


def test_the_guard_reports_every_dropped_key_not_just_the_first(island) -> None:
    """An operator acts on the whole list or not at all. Reporting one key at a time turns
    one refusal into N sequential rediscoveries of the same bug."""
    island.run()
    island.env_file.write_text(
        island.env_file.read_text()
        + "ISLAND_ID=enspyr\nISLAND_VERSION=0.9.3\nMODERATOR_USER_IDS=[\"op-1\"]\n"
    )
    out = island.run(expect_ok=False)
    combined = out.stdout + out.stderr
    for key in ("ISLAND_ID", "ISLAND_VERSION", "MODERATOR_USER_IDS"):
        assert key in combined, f"{key} was dropped from the report — the list must be total"
