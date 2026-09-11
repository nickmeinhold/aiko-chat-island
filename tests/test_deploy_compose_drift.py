"""The compose-drift preflight (claude-tasks#4230).

`update.sh` pulls an IMAGE and has never synced `docker-compose.yml`. On
2026-09-11 that took chat.enspyr.co down: `APNS_VOIP_TOPIC` became a required
member of config.py's all-or-none APNs group, the box's `.env` HAD the key, and
the box's compose did not forward it — so the value sat on the host and could not
reach the container. The box's compose differed from the tag by EXACTLY ONE LINE.
imagineering had the identical gap and deployed clean, because its compose
happened to get synced first. Same change, same drift, opposite outcome, one
variable: which files the operator happened to be thinking about.

`deploy/preflight-apns.sh` was the safety net that should have caught it. The
copy ON THE BOX was dated Sep 1 and contained zero references to
`APNS_VOIP_TOPIC` — the check written to abort before a half-set deploy could not
see the key that would break it. Same root cause, one file over: nothing syncs
`deploy/` either. So this guard compares the WHOLE deploy surface, not just
compose; a stale preflight is the second half of the same outage.

BOTH CONTROLS ARE BUILT HERE. The arms that must go red (a one-line compose
drift, a stale deploy script) and the arms that must stay green (identical trees,
a box that legitimately carries a SUBSET of the tag's deploy/, a box littered
with `.bak-*` files the tag has never heard of). The green arms are not padding:
both live islands are byte-identical to v0.11.0 right now, so a guard that
refused on a clean box would block every deploy from the day it landed.

FOUR EPISTEMIC STATES, NOT TWO. The script must never collapse "the ref does not
exist" (absence), "the network refused" (error), "the trees match" (silence) and
"a file differs" (refusal). Each gets its own exit status and its own message,
and each has a test — `curl -sf` swallowing a 403 and reading it as "no release"
is a failure this repo has already paid for once.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "preflight-compose-drift.sh"

# Exit statuses, named so a test reads as a claim about a STATE rather than about
# a number. 2 is deliberately not 1: "I could not look" and "I looked and it
# differs" are different facts, and update.sh says different things about them.
CLEAN = 0
DRIFT = 1
CANNOT_LOOK = 2

# The literal line whose absence from enspyr's compose caused the outage. Kept
# verbatim rather than paraphrased: the test is a reproduction, not an analogy.
THE_MISSING_LINE = "      APNS_VOIP_TOPIC: ${APNS_VOIP_TOPIC:-}"


def _run(box: Path, ref_tree: Path | None, ref: str = "v9.9.9", **env_extra):
    """Drive the script with the network seam closed.

    ISLAND_REF_TREE substitutes a local directory for the fetched tag. It is a
    TEST AND OFFLINE SEAM, not a bypass — the comparison still runs in full; only
    where the tag's bytes come from changes. The fetch half is proven separately
    by the network-marked test below, because a seam that skipped the comparison
    would let this whole file pass against a script that checks nothing.
    """
    env = {**os.environ, **env_extra}
    if ref_tree is not None:
        env["ISLAND_REF_TREE"] = str(ref_tree)
    return subprocess.run(
        [str(SCRIPT), str(box), ref], capture_output=True, text=True, env=env
    )


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


# The smallest shape that is recognisably an island deploy tree: the compose file
# the outage was about, plus the preflight that failed to catch it.
BASELINE = {
    "docker-compose.yml": "services:\n  chat-island:\n    environment:\n" + THE_MISSING_LINE + "\n",
    "deploy/update.sh": "#!/usr/bin/env bash\necho update\n",
    "deploy/preflight-apns.sh": "#!/usr/bin/env bash\necho apns\n",
    "deploy/lib/dotenv-read.sh": "dotenv_read() { :; }\n",
}


def test_identical_trees_are_clean(tmp_path) -> None:
    """THE ARM THAT MUST STAY GREEN, and the one both live boxes are in today.

    If this ever goes red the guard is unshippable — it would refuse every deploy
    of a correctly-synced island, and a check that blocks the healthy case gets
    commented out within a week."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", BASELINE)
    result = _run(box, tag)
    assert result.returncode == CLEAN, result.stderr


def test_the_one_line_compose_drift_that_caused_the_outage_refuses(tmp_path) -> None:
    """THE ARM THAT MUST GO RED — a byte-level reproduction of 2026-09-11.

    The box's compose is the tag's compose minus the APNS_VOIP_TOPIC forward. This
    is the exact state enspyr was in when the deploy dropped it, and the exact
    state the guard exists to refuse."""
    box_files = dict(BASELINE)
    box_files["docker-compose.yml"] = box_files["docker-compose.yml"].replace(
        THE_MISSING_LINE + "\n", ""
    )
    box = _tree(tmp_path / "box", box_files)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == DRIFT, "a compose that cannot forward a required key must abort"
    # Naming the FILE is the minimum; showing the LINE is what turns a refusal into
    # a fix. An operator reading "your compose differs" still has to go and diff it,
    # and this fires while they are trying to ship something else.
    assert "docker-compose.yml" in result.stderr, result.stderr
    assert "APNS_VOIP_TOPIC" in result.stderr, result.stderr


def test_a_stale_deploy_script_refuses_too(tmp_path) -> None:
    """The second half of the same outage. preflight-apns.sh ON THE BOX predated
    the key it needed to check for, so the net that existed to catch the compose
    gap was itself drifted. Compose alone would have shipped a guard that still
    misses half of what happened."""
    box_files = dict(BASELINE)
    box_files["deploy/preflight-apns.sh"] = "#!/usr/bin/env bash\necho stale\n"
    box = _tree(tmp_path / "box", box_files)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == DRIFT, result.stdout + result.stderr
    assert "preflight-apns.sh" in result.stderr, result.stderr


def test_a_nested_deploy_lib_file_is_compared(tmp_path) -> None:
    """deploy/lib/dotenv-read.sh is the single reader for every .env value the
    deploy scripts touch, and a drifted copy of it mis-reads a signing seed. The
    walk has to be recursive, not one level of deploy/."""
    box_files = dict(BASELINE)
    box_files["deploy/lib/dotenv-read.sh"] = "dotenv_read() { echo WRONG; }\n"
    box = _tree(tmp_path / "box", box_files)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == DRIFT, result.stdout + result.stderr
    assert "dotenv-read.sh" in result.stderr, result.stderr


def test_a_tag_file_the_box_does_not_carry_warns_but_does_not_refuse(tmp_path) -> None:
    """MEASURED, not assumed: the live boxes carry four files under deploy/ where
    the repo carries twelve. standup.sh is a first-standup tool and has no reason
    to sit on a running island; deploy/secrets/ is not shipped to the box at all.

    A whole-directory diff would refuse on both, every time, on both islands. So
    absence is a WARNING — it is real information (a box may be missing something
    it now needs) but it is not evidence of the drift class that caused the
    outage, which was a file present in both and DIFFERENT."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", {**BASELINE, "deploy/standup.sh": "#!/usr/bin/env bash\n"})

    result = _run(box, tag)

    assert result.returncode == CLEAN, "a legitimately-absent file must not block a deploy"
    assert "standup.sh" in result.stderr, "...but it must be named, not silently passed"


def test_box_only_files_are_ignored(tmp_path) -> None:
    """Both live boxes are carpeted in .env.bak-* and update.sh.bak-pre-v0110
    files, none of which the tag has ever heard of. Reporting them would bury the
    one line that matters under twenty that do not."""
    box = _tree(
        tmp_path / "box",
        {**BASELINE, "deploy/update.sh.bak-pre-v0110": "old\n", ".env.bak-v0100": "x\n"},
    )
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == CLEAN, result.stderr
    assert "bak" not in result.stderr, result.stderr


def test_a_missing_compose_on_the_box_refuses(tmp_path) -> None:
    """Distinct from a drifted one, and it must not read as 'nothing to compare'.
    update.sh already dies if compose is absent, so this is belt-and-braces — but
    the failure mode being guarded is the comparison quietly finding zero files
    and reporting CLEAN."""
    box_files = {k: v for k, v in BASELINE.items() if k != "docker-compose.yml"}
    box = _tree(tmp_path / "box", box_files)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == DRIFT, result.stdout + result.stderr
    assert "docker-compose.yml" in result.stderr, result.stderr


def test_an_unresolvable_ref_is_CANNOT_LOOK_not_CLEAN(tmp_path) -> None:
    """ABSENCE MUST NOT READ AS SILENCE. If the tag's bytes cannot be obtained, the
    script has learned nothing about the box — and 'I could not look' is the one
    answer that must never be spelled the same way as 'I looked and it matched'.

    This is the shape that has already cost this repo once: `curl -sf` swallowing
    a 403 and being read as 'no release exists'."""
    box = _tree(tmp_path / "box", BASELINE)
    result = _run(box, tmp_path / "does-not-exist")

    assert result.returncode == CANNOT_LOOK, result.stdout + result.stderr
    assert result.returncode != CLEAN


def test_the_comparison_is_not_vacuous(tmp_path) -> None:
    """THE CONTROL ON THE CONTROL. Every green arm above would also pass against a
    script that compared nothing at all, so the script reports how many files it
    actually examined and this asserts the count is the real one.

    A check whose disabled value equals its success value cannot report its own
    absence — which is exactly how a gateless step produces a byte-identical
    result to a clean run."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == CLEAN, result.stderr
    # THE FULL STRING, not a prefix. `f"{4} file"` is a substring of "14 file(s)",
    # "24 file(s)" and "104 file(s)" — so the one assertion whose entire job is to
    # prove the comparison was not a no-op was itself satisfied by a whole family
    # of wrong counts. A control that passes for reasons other than the one it
    # names is the defect this test exists to catch, committed by the test.
    assert f"{len(BASELINE)} file(s) compared" in result.stdout, (
        "the script must state how many files it compared, or a no-op passes as a pass: "
        + result.stdout
    )


def test_an_EMPTY_ref_tree_is_CANNOT_LOOK_not_CLEAN(tmp_path) -> None:
    """THE VACUOUS-COMPARISON ARM (cage-match round 1, Maxwell).

    A tree that materialises but holds nothing makes every per-file compare take
    its "the ref does not have this file" exit, so the walk finds no matches,
    nothing drifts, and the script would exit 0 having compared literally zero
    files. That is "I could not look" spelled exactly like "I looked and it
    matched" — the same fail-open as `die` inside a subshell, one function over.

    Forced, not observed: the ref tree is real and readable and simply empty."""
    box = _tree(tmp_path / "box", BASELINE)
    empty = tmp_path / "empty-tag"
    empty.mkdir()

    result = _run(box, empty)

    assert result.returncode == CANNOT_LOOK, (
        "an empty ref tree must not read as a clean comparison: "
        + result.stdout
        + result.stderr
    )


def test_a_ref_tree_with_no_compose_is_CANNOT_LOOK(tmp_path) -> None:
    """Narrower sibling of the above, and the one that actually fires in the wild:
    a tarball that unpacked with the wrong number of leading path components. The
    deploy files line up, `docker-compose.yml` does not, and a compose-blind pass
    would report clean on the one file the whole check is named for."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(
        tmp_path / "tag", {k: v for k, v in BASELINE.items() if k != "docker-compose.yml"}
    )

    result = _run(box, tag)

    assert result.returncode == CANNOT_LOOK, result.stdout + result.stderr


def test_a_symlinked_deploy_file_is_still_compared(tmp_path) -> None:
    """`find -type f` EXCLUDES symlinks (cage-match round 1, Maxwell + Carnot
    independently). A box with `deploy/update.sh -> /opt/island/update.sh` would
    drop that file from the compared set silently, and its drift would report as
    a clean pass — a hole in the guard, in the population of boxes most likely to
    have a hand-rolled layout.

    The arm forces the bad state: the symlink's TARGET is drifted, so a walk that
    skips symlinks returns CLEAN and only a walk that follows them returns
    DRIFT."""
    box = _tree(tmp_path / "box", {k: v for k, v in BASELINE.items() if k != "deploy/update.sh"})
    real = tmp_path / "elsewhere-update.sh"
    real.write_text("#!/usr/bin/env bash\necho STALE\n")
    (box / "deploy" / "update.sh").symlink_to(real)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == DRIFT, (
        "a symlinked deploy file whose target is stale must not read as clean: "
        + result.stdout
        + result.stderr
    )
    assert "update.sh" in result.stderr, result.stderr


def test_a_broken_symlink_under_deploy_refuses(tmp_path) -> None:
    """Follow-on from the same change: once symlinks are enumerated, one pointing
    at nothing fails the `-f` test. Refusing is the right answer — a deploy file
    that resolves to nothing is not a box in a state anyone should pull onto."""
    box = _tree(tmp_path / "box", {k: v for k, v in BASELINE.items() if k != "deploy/update.sh"})
    (box / "deploy" / "update.sh").symlink_to(tmp_path / "no-such-target")
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == DRIFT, result.stdout + result.stderr


def test_a_newline_in_a_filename_does_not_split_the_walk(tmp_path) -> None:
    """A line-oriented read of `find` output splits one path into two on an
    embedded newline, and both halves silently miss their comparison (cage-match
    round 1, Kelvin). Nothing in this repo carries such a name today — which is
    the same "today" that produced the outage this file refuses.

    The arm forces it: the weird-named file is DRIFTED, so a split walk reports
    clean and only a NUL-delimited walk reports drift."""
    weird = "deploy/we ird\nname.sh"
    box = _tree(tmp_path / "box", {**BASELINE, weird: "box version\n"})
    tag = _tree(tmp_path / "tag", {**BASELINE, weird: "tag version\n"})

    result = _run(box, tag)

    assert result.returncode == DRIFT, (
        "a drifted file whose name contains a newline must still be caught: "
        + result.stdout
        + result.stderr
    )


def test_a_bare_version_is_normalised_to_a_tag(tmp_path) -> None:
    """`ISLAND_VERSION=0.11.0` on both live boxes; the git tag is `v0.11.0`.

    That rewrite is on the path of EVERY real deploy and used to live inline in
    update.sh, where nothing could drive it (Tesla, cage-match round 2). Delete
    the arm and every deploy 404s into "could not look"; worse, a later change
    that treats 404 as an empty tree turns it CLEAN. Here it is one line with one
    test."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag, ref="0.11.0")

    assert result.returncode == CLEAN, result.stderr
    assert "v0.11.0" in result.stdout, (
        "a bare version must be reported as the TAG it will be resolved as, not as "
        "the string the operator typed: " + result.stdout
    )


def test_a_branch_ref_is_NOT_given_a_v_prefix(tmp_path) -> None:
    """The must-not-fire half of the same rule. `edge` is update.sh's documented
    default and tracks main; rewriting it to `vedge` would 404 every unpinned
    box. A normaliser with no arm proving what it LEAVES ALONE is half-tested."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag, ref="edge")

    assert result.returncode == CLEAN, result.stderr
    assert "vedge" not in result.stdout + result.stderr, result.stdout


def test_the_local_tree_seam_announces_itself(tmp_path) -> None:
    """ISLAND_REF_TREE substitutes a local directory for the fetched tag, and an
    ambient variable that silently redefines what a check MEANS is the worst
    shape this seam could have (Carnot, cage-match round 2): the run still prints
    "N file(s) compared against v0.11.0" while having compared against whatever
    was lying around. update.sh strips it so the deploy path cannot reach it at
    all; every other caller must be told."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == CLEAN, result.stderr
    assert "ISLAND_REF_TREE" in result.stderr, (
        "the seam must name itself in the output, or a local-tree comparison is "
        "indistinguishable from a real tag comparison: " + result.stderr
    )


def test_update_sh_STRIPS_the_local_tree_seam_before_invoking() -> None:
    """Regression pin, same honest caveat as the fail-closed pin above: a textual
    instrument reading the file a textual edit would change.

    Pinned because the defect is silent and total — an ISLAND_REF_TREE inherited
    from a host environment would make every deploy compare the box against a
    stale local tree and report clean, while still printing the tag's name."""
    update_sh = (REPO / "deploy" / "update.sh").read_text()
    assert "env -u ISLAND_REF_TREE" in update_sh, (
        "update.sh must strip ISLAND_REF_TREE before invoking the guard, so the "
        "deploy path structurally cannot compare against a local tree"
    )


@pytest.mark.skipif(
    os.environ.get("ISLAND_SKIP_NETWORK_TESTS") == "1" or shutil.which("curl") is None,
    reason="needs outbound network to codeload.github.com",
)
def test_the_real_fetch_resolves_a_real_tag(tmp_path) -> None:
    """POSITIVE CONTROL ON THE INSTRUMENT'S ONE UNTESTED HALF.

    Every other test in this file closes the network seam, so all of them would
    pass against a fetcher that never worked. This one drives the actual path an
    island box takes: resolve v0.11.0 from the public repo over the wire, and
    compare THIS checkout against it.

    THE CONTROL'S OWN VALIDITY (Tesla, cage-match round 2): an earlier version
    inherited the ambient environment, so a leaked ISLAND_REF_TREE would have
    made it pass WITHOUT EVER FETCHING — a positive control that is green for the
    one reason it exists to rule out. The env is scrubbed explicitly, and the
    output is asserted to show a real comparison rather than only a tolerable
    exit code."""
    env = {k: v for k, v in os.environ.items() if k != "ISLAND_REF_TREE"}
    result = subprocess.run(
        [str(SCRIPT), str(REPO), "v0.11.0"], capture_output=True, text=True, env=env
    )
    assert result.returncode in (CLEAN, DRIFT), (
        "the live fetch must resolve v0.11.0; CANNOT_LOOK here means the fetch path is "
        "broken and every other test in this file is measuring nothing. "
        + result.stdout
        + result.stderr
    )
    assert "ISLAND_REF_TREE" not in result.stderr, "the seam leaked into the control"
    # It must have actually COMPARED something. A fetch that yields an empty tree
    # now dies, but asserting the count keeps this control bound to real work
    # rather than to a tolerable exit code.
    assert "file(s) compared against v0.11.0" in result.stdout, result.stdout
    compared = int(result.stdout.split(" file(s) compared")[0].split(": ")[-1])
    assert compared > 3, f"only {compared} files compared against the real tag"


@pytest.mark.skipif(
    os.environ.get("ISLAND_SKIP_NETWORK_TESTS") == "1" or shutil.which("curl") is None,
    reason="needs outbound network to codeload.github.com",
)
def test_a_nonexistent_tag_over_the_WIRE_is_CANNOT_LOOK(tmp_path) -> None:
    """THE MUST-FAIL ARM FOR THE FETCH PATH (Tesla, cage-match round 2).

    The unresolvable-ref arm above closes the network seam, so it proves only
    that a missing DIRECTORY is handled — it never touches curl, an HTTP status,
    or the 404 branch. Those are the lines whose whole job is to keep "this ref
    does not exist" from being spelled the way "the trees match" is spelled, and
    nothing was driving them.

    This one asks the real server for a tag that cannot exist and requires the
    answer to be CANNOT_LOOK, naming the ref."""
    env = {k: v for k, v in os.environ.items() if k != "ISLAND_REF_TREE"}
    result = subprocess.run(
        [str(SCRIPT), str(REPO), "v0.0.0-does-not-exist"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == CANNOT_LOOK, (
        "a 404 from the real server must not read as a clean comparison: "
        + result.stdout
        + result.stderr
    )
    assert "v0.0.0-does-not-exist" in result.stderr, result.stderr


def test_update_sh_FAILS_CLOSED_when_the_guard_is_missing() -> None:
    """The partial-sync branch, pinned (cage-match round 1, Carnot).

    `update.sh` requires docker and a running island, so its branches cannot be
    driven from here. This is therefore a REGRESSION PIN, not a behavioural
    proof, and the difference matters: it is a textual instrument reading the
    same file a textual edit would change, so it shares that edit's blind spot
    and cannot see a branch that is correct in source and wrong in effect.

    It is still worth having. The defect it pins is specific and has already
    happened once in this file's history: the missing-guard branch was first
    written as `warn`-and-continue, copied from the APNs preflight above without
    re-deriving why that one warns. The APNs branch warns because an OLD
    update.sh can legitimately reach it; this branch cannot, because the guard
    ships in the same commit as the code that calls it — so reaching it means a
    partial sync, the exact failure this PR exists to refuse, and warning would
    make the check skippable without `--skip-drift-check`.

    A future edit that relaxes it back to a warning is the regression, and it
    would look entirely reasonable in a diff."""
    update_sh = (REPO / "deploy" / "update.sh").read_text()

    marker = "deploy-tree drift check NOT FOUND"
    assert marker in update_sh, (
        "the missing-guard branch has been renamed or removed — re-point this pin "
        "at whatever replaced it rather than deleting it"
    )
    branch = update_sh[update_sh.index(marker) :]
    # The `die` must be the thing that REPORTS this state, not a die somewhere
    # further down the file: take only up to the end of that message block.
    head = branch[: branch.index("fi")]
    assert "--skip-drift-check" in head, (
        "a fail-closed branch must name its escape hatch, or the operator's only "
        "route past a legitimately-blocked deploy is to edit the script"
    )
    assert "die " in update_sh[max(0, update_sh.index(marker) - 200) : update_sh.index(marker)], (
        "the missing-guard branch must FAIL CLOSED (die), not warn-and-continue: "
        "this update.sh ships with the guard, so the guard's absence means a "
        "partial sync — the very failure the guard exists to refuse"
    )
