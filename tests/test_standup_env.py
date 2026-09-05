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


# The literal prose the refusal always prints, regardless of which keys were found. Any
# key named in here is unusable as a fixture for the totality assertion below.
_REFUSAL_PROSE = (
    (Path(__file__).resolve().parents[1] / "deploy" / "standup.sh").read_text()
)


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
    # Keys standup genuinely does not write, and NONE of which appear in the refusal's
    # fixed prose. Two earlier fixtures were void: ISLAND_ID is read back now (so it tests
    # the read-back, not the report), and ISLAND_VERSION is NAMED IN THE BOILERPLATE
    # ("ISLAND_VERSION reverting to the compose default…"), so asserting it as a substring
    # passed whether or not the guard ever listed it — a check paid for by the message it
    # was checking (Tesla, cage-match #159).
    before = island.env_file.read_text() + (
        "APNS_TEAM_ID=T1\nGITHUB_CLIENT_ID=gh-1\nMODERATOR_USER_IDS=[\"op-1\"]\n"
    )
    island.env_file.write_text(before)
    for boilerplate_key in ("APNS_TEAM_ID", "GITHUB_CLIENT_ID", "MODERATOR_USER_IDS"):
        assert boilerplate_key not in _REFUSAL_PROSE, (
            f"{boilerplate_key} appears in the refusal's fixed text, so asserting it below "
            "would prove nothing about the reported list"
        )

    out = island.run(expect_ok=False)
    assert out.returncode != 0, "the guard did not fire at all"
    combined = out.stdout + out.stderr
    for key in ("APNS_TEAM_ID", "GITHUB_CLIENT_ID", "MODERATOR_USER_IDS"):
        assert key in combined, f"{key} was dropped from the report — the list must be total"
    assert island.env_file.read_text() == before, "the refused run mutated .env"


# --- ISLAND_ID survives a re-run (cage-match #159, Carnot) --------------------
#
# The guide tells a manual operator to set ISLAND_ID; standup never emitted one; the
# key-loss guard refuses any .env holding a key standup does not write. Those three
# together turned the documented manual path into one that could never be re-run — a
# doc change and a guard interacting, neither wrong alone.

def test_a_guide_written_island_id_survives_a_rerun(island) -> None:
    island.run()
    island.env_file.write_text(island.env_file.read_text() + "ISLAND_ID=example\n")

    island.run()   # must SUCCEED: the guard has nothing to complain about any more
    assert _values(island)["ISLAND_ID"] == "example", (
        "the operator's explicit island identity was dropped by a re-run — the id "
        "silently reverts to being derived from the base-url host, which re-namespaces "
        "this island's LiveKit rooms"
    )


def test_the_island_id_readback_test_can_actually_fail(island) -> None:
    """MUST-FAIL ARM. A fresh island sets no ISLAND_ID, so the key must be ABSENT rather
    than present-and-empty — otherwise the assertion above could pass against a line
    standup emits unconditionally, proving nothing about read-back."""
    island.run()
    assert "ISLAND_ID" not in _values(island), (
        "standup emitted an ISLAND_ID for an island that never set one — read-back "
        "became a default, which is #3835's call to make, not this guard's"
    )


# --- an abort must leave NO deploy state changed (cage-match #159, Carnot) ----
#
# The key-loss guard sits near the END of standup, and the --with-media path used to
# install deploy/livekit/.env near the START. So "refused" meant "refused, but the media
# half already landed" — a partial mutation wearing a clean abort's clothes. The SFU env
# is now STAGED and renamed into place only after the last gate, under an EXIT trap.

def _media_island(island):
    """Give curl a public IP so --with-media can run. The default stub returns nothing,
    which standup correctly treats as fatal (a blank node_ip advertises an unreachable
    ICE candidate and every call fails at CONNECT, silently)."""
    stub = island.root.parent / "bin" / "curl"
    stub.write_text('#!/usr/bin/env bash\ncase "$*" in *ipify*) echo 203.0.113.9 ;; esac\nexit 0\n')
    stub.chmod(0o755)
    return island.root / "deploy" / "livekit" / ".env"


def test_a_refused_rerun_does_not_install_the_sfu_env(island) -> None:
    lk_env = _media_island(island)
    island.run("--with-media", "--livekit-domain", "livekit.example.org")
    assert lk_env.exists(), "fixture void: --with-media must have produced an SFU env"
    # INODE, not content. The SFU env is deterministic given the same inputs, so a re-run
    # rewrites it BYTE-IDENTICALLY — a content comparison passes whether or not the file
    # was replaced, making it a check whose outcome is independent of the thing it checks.
    # Caught by running this test against the pre-fix ordering, where it stayed green.
    # `mv` installs a new inode, so the inode is what actually witnesses the install.
    lk_before_ino = lk_env.stat().st_ino

    island.env_file.write_text(island.env_file.read_text() + "APNS_PRIVATE_KEY=only-here\n")
    result = island.run("--with-media", "--livekit-domain", "livekit.example.org",
                        expect_ok=False)

    assert result.returncode != 0, "the guard did not fire"
    assert lk_env.stat().st_ino == lk_before_ino, (
        "the refused run still installed a new SFU env — an abort changed deploy state, "
        "so 'refused' does not mean 'nothing happened'"
    )


def test_a_refused_rerun_strands_no_temp_file_holding_the_sfu_secret(island) -> None:
    """Staging moves the SFU file's install to the end of the run, which opens a window
    where an abort could leave a mode-600 mktemp file holding LIVEKIT_API_SECRET beside
    the real one. The EXIT trap has to close it on EVERY exit, not just the guard's."""
    lk_env = _media_island(island)
    island.run("--with-media", "--livekit-domain", "livekit.example.org")
    island.env_file.write_text(island.env_file.read_text() + "APNS_PRIVATE_KEY=only-here\n")
    island.run("--with-media", "--livekit-domain", "livekit.example.org", expect_ok=False)

    # mktemp's suffix is exactly six chars, so this cannot collide with the tracked
    # .env.example the fixture copies in — a wider glob matched it and failed for the
    # wrong reason.
    strays = [p.name for p in lk_env.parent.glob(".env.??????")] + \
             [p.name for p in island.env_file.parent.glob(".env.??????")]
    assert not strays, f"a staged secret-bearing temp file survived the abort: {strays}"


# --- the guard's own failure modes (cage-match #159, Tesla) -------------------

def test_a_key_the_reader_could_read_is_not_invisible_to_the_lister(island) -> None:
    """The lister must OVER-approximate, because the two directions are not symmetric: a
    name it misses is destroyed silently, a name it invents only causes a readable refusal.
    dotenv_read interpolates whatever key its caller asks for and has no charset at all, so
    a lister with a narrow `[A-Z_]+` charset would report `FOO.BAR` as safe to delete."""
    island.run()
    before = island.env_file.read_text() + "APNS.TEAM-ID=T1\n"
    island.env_file.write_text(before)
    out = island.run(expect_ok=False)
    assert out.returncode != 0, "a dotted/hyphenated key was invisible and would be destroyed"
    assert "APNS.TEAM-ID" in (out.stdout + out.stderr)
    assert island.env_file.read_text() == before


def test_a_commented_out_assignment_is_not_reported_as_a_key(island) -> None:
    """The null arm of the widening. Over-approximation is the safe direction, but it stops
    at comments — otherwise a `#OLD_KEY=x` left in a .env would brick every re-run, and a
    guard that refuses on a comment gets disabled by the first operator who meets it."""
    island.run()
    island.env_file.write_text(island.env_file.read_text() + "#RETIRED_KEY=old\n# X=1\n")
    island.run()   # must SUCCEED


def test_the_key_listing_propagates_a_read_error_instead_of_reporting_no_keys(island) -> None:
    """`grep | sed | sort` returns the STATUS OF SORT, so a grep that died of an I/O error
    reads as 'this file assigns nothing' — which the comparison then treats as 'nothing to
    preserve'. That is the non-match-is-absence shape deploy/lib/dotenv-read.sh exists to
    kill, committed by the function enforcing it. grep: 0 matched, 1 no-match (legitimate),
    >=2 real error."""
    lib = island.root / "deploy" / "lib" / "dotenv-read.sh"
    unreadable = island.root / "locked.env"
    unreadable.write_text("SECRET=x\n")
    unreadable.chmod(0o000)
    try:
        r = subprocess.run(
            ["bash", "-c", f'source "{lib}"; dotenv_keys "{unreadable}"'],
            capture_output=True, text=True)
        assert r.returncode != 0, (
            "an unreadable file listed as zero keys with a SUCCESS status — the caller "
            "would read that as 'nothing to preserve' and destroy everything in it"
        )
    finally:
        unreadable.chmod(0o600)


def test_an_empty_but_valid_env_lists_no_keys_and_succeeds(island) -> None:
    """NULL ARM for the test above: 'no assignments' must stay a SUCCESS, or a brand-new
    island — which legitimately has no .env — could never stand up."""
    lib = island.root / "deploy" / "lib" / "dotenv-read.sh"
    empty = island.root / "empty.env"
    empty.write_text("# just a comment\n")
    r = subprocess.run(["bash", "-c", f'source "{lib}"; dotenv_keys "{empty}"'],
                       capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == ""


def test_a_failing_key_listing_aborts_instead_of_rewriting(island) -> None:
    """THE FAIL-OPEN THIS GUARD ALMOST SHIPPED WITH.

    The first draft compared `<(dotenv_keys A)` against `<(dotenv_keys B)`. Process
    substitution DISCARDS the child's exit status even under `set -euo pipefail`, so a
    lister that died produced an empty fifo, `comm` compared nothing against nothing, the
    dropped set was empty, and the rewrite proceeded — a comparison that never happened,
    wearing the clothes of "nothing to preserve". This script already documents that exact
    swallow eighty lines above, for the resolver.

    The fixture's deploy/ is a COPY, so the lister can be broken here without touching the
    real one."""
    island.run()
    before = island.env_file.read_text() + "ISLAND_SIGNING_SEED=not-recoverable\n"
    island.env_file.write_text(before)

    lib = island.root / "deploy" / "lib" / "dotenv-read.sh"
    lib.write_text(lib.read_text() + '\ndotenv_keys() { return 3; }\n')

    result = island.run(expect_ok=False)
    assert result.returncode != 0, (
        "the key listing FAILED and standup rewrote .env anyway — the guard did not run, "
        "and its silence was read as 'nothing to preserve'"
    )
    assert island.env_file.read_text() == before, "the signing seed was destroyed"


def test_the_failing_listing_test_can_actually_pass(island) -> None:
    """NULL ARM. With the lister intact and the same extra key present, the run must fail
    for the RIGHT reason — naming the key — rather than for the broken-lister reason. Two
    different aborts are not the same abort."""
    island.run()
    island.env_file.write_text(island.env_file.read_text() + "ISLAND_SIGNING_SEED=x\n")
    out = island.run(expect_ok=False)
    assert "ISLAND_SIGNING_SEED" in (out.stdout + out.stderr), (
        "the refusal did not name the key, so the test above could be passing on a "
        "generic failure rather than on the guard"
    )


def test_a_failure_between_creating_and_writing_the_sfu_env_strands_nothing(island) -> None:
    """The EXIT trap can only remove a path it can NAME. An earlier revision created the
    staged SFU file with `mktemp` and recorded it in the trap-visible variable several
    lines later, after the heredoc and the chmod — so a failure in between left a mode-600
    file holding LIVEKIT_API_SECRET beside the real one, invisible to the trap (Carnot,
    cage-match #159 round 2). The window is closed by assigning the tracked name AT the
    mktemp; this proves it by failing inside the window."""
    lk_env = _media_island(island)
    script = island.root / "deploy" / "standup.sh"
    script.write_text(script.read_text().replace(
        'LK_ENV_TARGET="$lk_env"\n  cat > "$LK_ENV_STAGED"',
        'LK_ENV_TARGET="$lk_env"\n  false\n  cat > "$LK_ENV_STAGED"', 1))

    island.run("--with-media", "--livekit-domain", "livekit.example.org", expect_ok=False)

    strays = [p.name for p in lk_env.parent.glob(".env.??????")]
    assert not strays, (
        f"a failure between creating and writing the staged SFU env stranded a "
        f"secret-bearing temp file the trap could not see: {strays}"
    )


def test_the_stranded_file_test_can_actually_fail(island) -> None:
    """NULL ARM. Injecting the same failure BEFORE the tracked assignment must strand a
    file — otherwise the test above could pass because the injection never fired, or
    because --with-media never reached the mktemp at all."""
    lk_env = _media_island(island)
    script = island.root / "deploy" / "standup.sh"
    script.write_text(script.read_text().replace(
        'LK_ENV_STAGED="$(mktemp "${lk_env}.XXXXXX")"',
        'LK_ENV_STAGED_UNTRACKED="$(mktemp "${lk_env}.XXXXXX")"\n  false', 1))

    island.run("--with-media", "--livekit-domain", "livekit.example.org", expect_ok=False)

    strays = [p.name for p in lk_env.parent.glob(".env.??????")]
    assert strays, (
        "the injection did not strand anything, so the test above proves nothing about "
        "the trap — it may simply never have reached the staging code"
    )


# --- the ONLY bypass names its keys (cage-match #159 round 3, Carnot) ---------
#
# The refusal CAN be wrong: dotenv_keys is line-oriented, so a multiline quoted value
# contributes phantom keys. A refusal that can be wrong with no bypass is a permanently
# dead documented path — over-approximation is safe for data LOSS and unsafe for
# AVAILABILITY. The bypass therefore exists, but must NAME every key, so it cannot be
# typed as a reflex and cannot be satisfied from a stale list.

def test_naming_every_at_risk_key_discharges_the_refusal(island) -> None:
    island.run()
    island.env_file.write_text(island.env_file.read_text() + "APNS_TEAM_ID=T1\nGITHUB_CLIENT_ID=g1\n")

    island.run("--drop-env-keys", "APNS_TEAM_ID,GITHUB_CLIENT_ID")   # must SUCCEED

    v = _values(island)
    assert "APNS_TEAM_ID" not in v and "GITHUB_CLIENT_ID" not in v, (
        "the keys were acknowledged for removal but survived — the flag did not do what "
        "the operator consented to"
    )


def test_naming_only_some_at_risk_keys_still_refuses(island) -> None:
    """THE ARM THAT MAKES IT AN ACKNOWLEDGEMENT AND NOT A FORCE. If a partial list passed,
    the flag would be `--force` wearing a longer name."""
    island.run()
    before = island.env_file.read_text() + "APNS_TEAM_ID=T1\nGITHUB_CLIENT_ID=g1\n"
    island.env_file.write_text(before)

    out = island.run("--drop-env-keys", "APNS_TEAM_ID", expect_ok=False)
    assert out.returncode != 0, "an un-named at-risk key was destroyed under a partial list"
    assert "GITHUB_CLIENT_ID" in (out.stdout + out.stderr), "the remaining key was not named"
    assert island.env_file.read_text() == before


def test_naming_a_key_that_is_not_at_risk_refuses(island) -> None:
    """Strict in the OTHER direction too. A stale or mistyped name means the list the
    operator is working from is not this run's, and consent given against the wrong list is
    not consent — it must not silently discharge nothing while reading as approval."""
    island.run()
    before = island.env_file.read_text()
    out = island.run("--drop-env-keys", "SOME_KEY_THAT_IS_NOT_THERE", expect_ok=False)
    assert out.returncode != 0
    assert "SOME_KEY_THAT_IS_NOT_THERE" in (out.stdout + out.stderr)
    assert island.env_file.read_text() == before


def test_the_bypass_is_not_needed_on_a_clean_rerun(island) -> None:
    """NULL ARM. A re-run with nothing at risk must succeed with no flag at all — without
    this, a standup that refused every re-run would satisfy the arms above."""
    island.run()
    island.run()


def test_a_nul_byte_does_not_blind_the_key_listing(island) -> None:
    """A NUL ANYWHERE IN .env USED TO DEFEAT THE LISTING ITSELF.

    Without `grep -a`, one NUL makes grep declare "Binary file … matches" and emit no
    matching lines — so this returns a single nonsense entry, or on a grep that instead
    exits 1, THE EMPTY SET WITH A SUCCESS STATUS. A caller comparing that against the file
    it is about to write computes "nothing would be dropped" and rewrites over a live
    signing seed. Not exotic: a value pasted out of a UTF-16 editor carries them. This is
    non-match-is-absence at the one input that is not an ASCII line — the ghost
    deploy/lib/dotenv-read.sh exists to bury (Tesla, cage-match #159 round 2).

    Tested at the LIBRARY, because that is where `grep -a` is the only variable. Driving
    it through standup proves less than it appears to: resolve-media-env.sh aborts on the
    same file first, so the run dies for an unrelated reason and the key-loss guard is
    never reached — incidental protection, not this guard working.
    """
    lib = island.root / "deploy" / "lib" / "dotenv-read.sh"
    env = island.root / "nul.env"
    env.write_bytes(b"ISLAND_SIGNING_SEED=not-recoverable\nJUNK=\x00\nAPNS_TEAM_ID=T1\n")

    r = subprocess.run(["bash", "-c", f'source "{lib}"; dotenv_keys "{env}"'],
                       capture_output=True, text=True)
    keys = set(r.stdout.split())
    assert r.returncode == 0, f"the listing failed outright: {r.stderr}"
    assert {"ISLAND_SIGNING_SEED", "APNS_TEAM_ID"} <= keys, (
        f"a NUL byte blinded the listing — got {keys or 'nothing'}. A caller would read "
        "that as 'this file assigns nothing to preserve' and destroy the seed."
    )


def test_a_nul_bearing_env_is_never_silently_rewritten(island) -> None:
    """The invariant that actually matters, asserted without caring WHICH guard fires.
    Something must stop a run against a file the tooling cannot fully read, and the file
    must survive. Today the media resolver aborts first; if that ever changes, the
    key-loss guard is now able to see the file itself."""
    island.run()
    before = island.env_file.read_bytes() + b"ISLAND_SIGNING_SEED=not-recoverable\nJUNK=\x00\n"
    island.env_file.write_bytes(before)

    result = island.run(expect_ok=False)
    assert result.returncode != 0, "a NUL-bearing .env was rewritten"
    assert island.env_file.read_bytes() == before, "the signing seed was destroyed"


def test_the_nul_test_can_actually_pass(island) -> None:
    """NULL ARM for the library test: the SAME file without the NUL must list the same
    keys, so the assertion above discriminates on the NUL rather than on the fixture."""
    lib = island.root / "deploy" / "lib" / "dotenv-read.sh"
    env = island.root / "clean.env"
    env.write_bytes(b"ISLAND_SIGNING_SEED=not-recoverable\nJUNK=x\nAPNS_TEAM_ID=T1\n")
    r = subprocess.run(["bash", "-c", f'source "{lib}"; dotenv_keys "{env}"'],
                       capture_output=True, text=True)
    assert {"ISLAND_SIGNING_SEED", "APNS_TEAM_ID"} <= set(r.stdout.split())
