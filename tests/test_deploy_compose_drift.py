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
import sys
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


def _compared_count(stdout: str) -> int:
    """The number of files the script says it compared, parsed as an INTEGER.

    A SUBSTRING CHECK CANNOT DO THIS JOB, and two rounds of cage-match were
    needed to make that stick. Round 1 caught `f"{4} file"` matching
    "14 file(s)"; the fix extended the needle to `"4 file(s) compared"` and
    declared in a capitalised comment that it was now THE FULL STRING — which is
    a substring of "14 file(s) compared" and "104 file(s) compared" in exactly
    the same way (Kelvin, round 5: the comment shouts one principle and the code
    implements the other).

    So the assertion that proves the comparison was not a no-op is now arithmetic
    rather than text. There is no third way for this to be a prefix."""
    marker = " file(s) compared against "
    assert marker in stdout, f"no comparison count in output: {stdout!r}"
    return int(stdout.split(marker)[0].rsplit(":", 1)[-1].strip())


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
    # Parsed as an integer, for the reason in _compared_count: every textual
    # form of this assertion has been a prefix match in disguise, twice.
    assert _compared_count(result.stdout) == len(BASELINE), (
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


def _update_sh_code() -> str:
    """update.sh with every comment line removed.

    THE POINT OF THIS FUNCTION IS A BUG IT ALREADY CAUGHT (Tesla, cage-match
    round 3). The seam pin below used to grep the whole file for
    `env -u ISLAND_REF_TREE` — a string that also appears in the COMMENT
    explaining why the command is there. Delete the actual command, keep the
    comment, and the test stayed green while every deploy compared against
    whatever local tree was lying around.

    A control satisfied by the prose describing the thing it checks does not
    depend on the thing it checks. That is the exact defect class this whole PR
    is about, committed inside its own verification."""
    text = (REPO / "deploy" / "update.sh").read_text()
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("seam", ["ISLAND_REF_TREE", "ISLAND_REPO_SLUG", "ISLAND_CODELOAD_BASE"])
def test_update_sh_STRIPS_every_ambient_seam_before_invoking(seam) -> None:
    """All three seams, as a class (Tesla, cage-match round 3).

    Each changes what "compared against v0.11.0" MEANS, and each is an ambient
    environment variable that can arrive on a host without appearing in any
    command line. An earlier pass stripped exactly one and left the siblings
    live — patching an instance of a class that had already been named.
    ISLAND_REPO_SLUG is the dangerous one: inherited, it fetches someone else's
    tarball, and if that tree matches the box the guard reports the trees match
    while `docker compose pull` still interpolates the real image.

    Read against comment-stripped source, for the reason in _update_sh_code."""
    assert f"-u {seam}" in _update_sh_code(), (
        f"update.sh must strip {seam} before invoking the guard, so the deploy "
        f"path structurally cannot be redirected by an inherited environment"
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
    assert _compared_count(result.stdout) > 3, (
        f"too few files compared against the real tag: {result.stdout}"
    )


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


def test_a_BOX_ONLY_compose_override_is_refused(tmp_path) -> None:
    """THE ONE BOX-ONLY FILE THAT CANNOT BE IGNORED (Maxwell, cage-match round 3).

    Every other box-only file is deliberately invisible to this guard — the live
    islands are carpeted in `.env.bak-*` and `update.sh.bak-pre-v0110`, and
    reporting them would bury the line that matters. An override file is the
    exception, and the reason is mechanical rather than stylistic.

    MEASURED on the live box: `docker compose config` with no `-f` flags merged a
    `docker-compose.override.yml` sitting beside the base file and emitted its
    variables into the resolved config. update.sh's deploy path is exactly that —
    a bare `docker compose pull && up` — so an override the tag has never heard
    of is silently part of what gets deployed. That is this guard's own subject
    matter arriving as an EXTRA file instead of an edited one, which is exactly
    why the box-only rule would have waved it through.

    Neither island carries one today. The point is that one appearing is
    invisible to every other check in the file."""
    box = _tree(tmp_path / "box", {**BASELINE, "docker-compose.override.yml": "services: {}\n"})
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == DRIFT, (
        "an auto-loaded override the tag does not know about must abort the deploy: "
        + result.stdout
        + result.stderr
    )
    assert "override" in result.stderr, result.stderr


def test_an_override_the_REF_also_carries_is_compared_not_flagged(tmp_path) -> None:
    """The must-NOT-fire half. Once the repo legitimately ships an override, it
    stops being box-only and becomes an ordinary compared file — flagging it
    forever would make the guard cry wolf on a correctly-synced box, which is how
    a guard gets deleted."""
    shipped = {**BASELINE, "docker-compose.override.yml": "services: {}\n"}
    box = _tree(tmp_path / "box", shipped)
    tag = _tree(tmp_path / "tag", shipped)

    result = _run(box, tag)

    assert result.returncode == CLEAN, result.stdout + result.stderr
    assert _compared_count(result.stdout) == len(shipped), (
        "a ref-carried override must be COMPARED, not merely tolerated: " + result.stdout
    )


def test_a_second_root_compose_file_is_compared(tmp_path) -> None:
    """"The compose file" is a convenient singular docker does not share. A
    drifted `docker-compose.build.yml` (or any future fragment) must be caught on
    the same terms as the base file."""
    extra = {**BASELINE, "docker-compose.build.yml": "services:\n  chat-island:\n    build: .\n"}
    box_files = dict(extra)
    box_files["docker-compose.build.yml"] = "services:\n  chat-island:\n    build: /wrong\n"
    box = _tree(tmp_path / "box", box_files)
    tag = _tree(tmp_path / "tag", extra)

    result = _run(box, tag)

    assert result.returncode == DRIFT, result.stdout + result.stderr
    assert "docker-compose.build.yml" in result.stderr, result.stderr


# Every non-POSIX flag the guard depends on, with the command that proves it.
# Adding a flag to the script without adding it here is the regression this
# catches — and the list is the CLASS, not a collection of past incidents.
_NON_POSIX_FLAGS = {
    "find -L": "find -L . -type f >/dev/null",
    "find -print0": "find . -print0 >/dev/null",
    "find -maxdepth": "find . -maxdepth 1 >/dev/null",
    "sort -z": r"printf 'b\0a\0' | LC_ALL=C sort -z >/dev/null",
    "read -d ''": r"""bash -c "while IFS= read -r -d '' x; do :; done < <(printf 'a\0')" """,
    "diff -u --label": "diff -u --label A --label B f1 f2; [ $? -le 1 ]",
    "cmp -s": "cmp -s f1 f1",
    "uniq -c": r"printf 'a\na\n' | uniq -c >/dev/null",
    "tar --no-same-owner": (
        "tar -czf t.tgz f1 && mkdir -p o && tar -xzf t.tgz -C o --no-same-owner"
    ),
}


@pytest.mark.parametrize("flag,probe", sorted(_NON_POSIX_FLAGS.items()))
def test_every_non_posix_flag_works_on_THIS_platform(flag, probe, tmp_path) -> None:
    """THE CLASS, closed rather than the instance patched (cage-match round 3).

    The guard runs on two userlands: the orchestrator's macOS BSD tools and the
    island boxes' GNU tools. A GNU-only flag does not error loudly there — it
    kills one pipeline and the walk silently yields nothing, which this script
    then reports as a clean comparison. A no-op wearing a pass.

    It happened: `sed -z` (BSD sed has none) made the multi-compose walk find
    zero files. Carnot had flagged the same CLASS for `sort -z` one round
    earlier; that specific claim was wrong — Apple's sort does support -z,
    measured twice — and I introduced a real instance of the class it named while
    rejecting the false instance. So the fix is the sweep, not the patch.

    THIS TEST IS HALF AN INSTRUMENT ON ITS OWN. It can only speak for the
    platform it runs on. What makes it cover both is WHERE it runs: CI is Linux,
    development is macOS, and the same parametrised list has to pass in both
    places before anything merges. Neither run is sufficient; the pair is."""
    (tmp_path / "f1").write_text("a\n")
    (tmp_path / "f2").write_text("b\n")
    result = subprocess.run(probe, shell=True, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, (
        f"`{flag}` is not available on this platform ({sys.platform}); the guard "
        f"depends on it and would silently compare nothing. stderr: {result.stderr}"
    )


def test_a_root_compose_file_the_box_does_not_need_is_a_WARNING(tmp_path) -> None:
    """THE ARM THAT WAS MISSING, and its absence reached production.

    The repo ships `docker-compose.build.yml` for `--from-source`. No island
    carries it, because islands are pull-based and never build. When the
    multi-compose walk first landed it ran the ref-only case through
    `compare_one`, which treats a missing box file as DRIFT — so BOTH clean live
    boxes exited 1, and the guard would have refused every healthy deploy from
    the day it merged.

    THIRTY-THREE UNIT TESTS WERE GREEN. Not one fixture had a root compose file
    present in the ref and absent on the box, so the whole suite was blind to it
    by construction; the only instrument that saw it was the null arm run against
    the real boxes. That is the finding, more than the bug: a fixture family can
    be exhaustive about the cases it contains and say nothing about the case it
    cannot express."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(
        tmp_path / "tag",
        {**BASELINE, "docker-compose.build.yml": "services:\n  chat-island:\n    build: .\n"},
    )

    result = _run(box, tag)

    assert result.returncode == CLEAN, (
        "a root compose file the box legitimately does not carry must not refuse "
        "the deploy: " + result.stdout + result.stderr
    )
    assert "docker-compose.build.yml" in result.stderr, (
        "...but it must still be named: " + result.stderr
    )


def test_a_refusal_SHOWS_the_diff_body_not_just_the_filename(tmp_path) -> None:
    """"Refuse and show" is the guard's stated contract and nothing asserted the
    SHOW half directly (cage-match rounds 2-4, Carnot, three times).

    Carnot's specific claim — that errexit aborts the block before the diff is
    printed — was refuted with a run on each platform. But the repetition was
    worth listening to on a different axis: the red path's correctness rested on
    a subtle rule about where errexit is disabled, and nothing in the suite would
    have noticed if a future edit broke it. An operator who gets a refusal naming
    a file, with no diff, has to go and diff it by hand at the exact moment they
    were trying to ship something else.

    Both halves are asserted here: the changed line's CONTENT, and the truncation
    notice on a divergence larger than the 40-line window."""
    long_box = "\n".join(str(i) for i in range(200)) + "\n"
    long_tag = "\n".join(str(i + 1000) for i in range(200)) + "\n"
    box = _tree(tmp_path / "box", {**BASELINE, "docker-compose.yml": long_box})
    tag = _tree(tmp_path / "tag", {**BASELINE, "docker-compose.yml": long_tag})

    result = _run(box, tag)

    assert result.returncode == DRIFT, result.stdout + result.stderr
    assert "@@" in result.stderr, "the unified diff body must reach the operator"
    assert "more diff lines" in result.stderr, (
        "a truncated diff must say how much was cut, not leave a silent tail"
    )
    assert "differs from" in result.stderr, "the refusal itself must still print"


def test_a_ref_tree_with_compose_but_NO_deploy_is_CANNOT_LOOK(tmp_path) -> None:
    """THE INTERSECTION-OF-ONE FAIL-OPEN (Tesla, cage-match round 3).

    A ref tree holding a matching `docker-compose.yml` and nothing else passes
    the compose-exists check, and then every script on the box is "a file the ref
    does not have" — box-only, ignored by design — so a drifted `update.sh` reads
    as CLEAN. The compose check certifies ONE FILE; nothing certified the SHAPE.

    GitHub's real archive always carries both, which is precisely why the happy
    path hides it. Measured before the fix: this exact tree exited 0 while the
    box's update.sh said DRIFTED."""
    box = _tree(tmp_path / "box", {**BASELINE, "deploy/update.sh": "DRIFTED\n"})
    tag = _tree(tmp_path / "tag", {"docker-compose.yml": BASELINE["docker-compose.yml"]})

    result = _run(box, tag)

    assert result.returncode == CANNOT_LOOK, (
        "a ref tree with no deploy/ cannot certify anything about the box's "
        "scripts, and must not say clean: " + result.stdout + result.stderr
    )
    # ASSERT THE MESSAGE, NOT ONLY THE CODE. Mutation-checked: with the explicit
    # shape check removed, the ref-side walk ALSO fails (find has no deploy/ to
    # read) and the run still exits 2 — so an exit-code-only assertion passes
    # against a script missing the check it is named for. Two mechanisms reaching
    # the same exit is defence in depth for the operator and a blind spot for the
    # test; pinning the sentence separates them.
    assert "did not materialise as this repository" in result.stderr, (
        "the shape check must be the thing that fires, with its own message: "
        + result.stderr
    )


def test_a_symlinked_DIRECTORY_under_deploy_is_traversed(tmp_path) -> None:
    """THE ARM THE -L FIX NEVER HAD (Tesla, cage-match round 3).

    `-L` was added in round 2 so `deploy/lib -> /opt/island/lib` is traversed
    rather than merely listed. The suite only ever forced a FILE symlink — which
    `-type l` already catches without `-L` — so removing `-L` left every test
    green. Mutation-checked: it did.

    Here the drifted file lives BEHIND a symlinked directory, so only a
    following walk can see it."""
    box = _tree(tmp_path / "box", {k: v for k, v in BASELINE.items() if not k.startswith("deploy/lib")})
    real_lib = tmp_path / "elsewhere-lib"
    real_lib.mkdir()
    (real_lib / "dotenv-read.sh").write_text("dotenv_read() { echo DRIFTED; }\n")
    (box / "deploy" / "lib").symlink_to(real_lib, target_is_directory=True)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag)

    assert result.returncode == DRIFT, (
        "a drifted file behind a symlinked directory must not read as clean: "
        + result.stdout
        + result.stderr
    )
    assert "dotenv-read.sh" in result.stderr, result.stderr


def _serve_once(tmp_path, body: bytes, status: int = 200):
    """A one-request local HTTP server, returning (base_url, thread).

    This exists so the FETCH path can be driven offline. Before the
    ISLAND_CODELOAD_BASE seam, the only way to reach curl+tar was the real
    codeload, which always serves a well-formed archive — so the "the archive is
    corrupt" branch and every non-200 status but 404 were unreachable by any
    test. Mutation-checked at the time: deleting the tar `|| die` left the whole
    suite green."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # silence
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_port}", srv


def test_a_CORRUPT_archive_is_CANNOT_LOOK(tmp_path) -> None:
    """THE THIRD EPISTEMIC STATE, finally driven (Tesla, cage-match round 3).

    "the ref does not exist", "the network refused" and "the archive is corrupt"
    are three different facts the script's prose distinguishes — and only the
    first two had arms. A truncated or non-gzip body reaching `tar` must be
    CANNOT_LOOK, never a clean comparison against an empty tree.

    Served locally rather than mocked, so curl and tar are the real ones."""
    box = _tree(tmp_path / "box", BASELINE)
    base, srv = _serve_once(tmp_path, b"this is definitely not a gzip stream")
    try:
        env = {k: v for k, v in os.environ.items() if k != "ISLAND_REF_TREE"}
        env["ISLAND_CODELOAD_BASE"] = base
        result = subprocess.run(
            [str(SCRIPT), str(box), "v1.2.3"], capture_output=True, text=True, env=env
        )
    finally:
        srv.shutdown()

    assert result.returncode == CANNOT_LOOK, result.stdout + result.stderr
    assert "CORRUPT" in result.stderr or "did not unpack" in result.stderr, result.stderr


def test_a_500_from_the_server_is_CANNOT_LOOK_and_names_the_status(tmp_path) -> None:
    """The `*)` arm of the HTTP branch — everything that is neither 200 nor 404.

    `curl -sf` swallowing a 403 and reading as "no release exists" is a shape
    this repo has already paid for once, and the branch written to prevent it
    had no test."""
    box = _tree(tmp_path / "box", BASELINE)
    base, srv = _serve_once(tmp_path, b"nope", status=500)
    try:
        env = {k: v for k, v in os.environ.items() if k != "ISLAND_REF_TREE"}
        env["ISLAND_CODELOAD_BASE"] = base
        result = subprocess.run(
            [str(SCRIPT), str(box), "v1.2.3"], capture_output=True, text=True, env=env
        )
    finally:
        srv.shutdown()

    assert result.returncode == CANNOT_LOOK, result.stdout + result.stderr
    assert "500" in result.stderr, "the status must be named, not folded into a generic error"


def test_an_ambient_seam_ANNOUNCES_itself(tmp_path) -> None:
    """Every seam must say so. update.sh strips all three, but any other caller
    (a human at a shell, a future script) must be told that a clean result does
    not mean what an unconfigured clean result means."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", BASELINE)

    result = _run(box, tag, ISLAND_REPO_SLUG="someone/else")

    assert "ISLAND_REPO_SLUG" in result.stderr, (
        "a seam that can be silent is not a seam, it is a trapdoor: " + result.stderr
    )


def test_the_https_pin_is_applied_when_no_seam_is_set() -> None:
    """The protocol pin is SCOPED to the default host, so prove it still binds
    there. Two halves, because either alone is weak:

    1. the flags are on the default path (read from comment-stripped source, for
       the reason in _update_sh_code — a pin satisfied by prose explaining it is
       not a pin);
    2. the flag combination actually refuses http, driven against curl itself
       rather than assumed from the manpage.

    Scoping a security control to make a test pass would be the wrong trade. The
    seam that relaxes it is announced by the guard and stripped by update.sh, so
    it cannot reach a deploy — but none of that matters if the default stopped
    pinning."""
    code = "\n".join(
        ln
        for ln in (REPO / "deploy" / "preflight-compose-drift.sh").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "--proto =https --proto-redir =https" in code, (
        "the default fetch path must pin https; -L alone will follow a redirect "
        "down to plain HTTP"
    )
    assert 'ISLAND_CODELOAD_BASE:-}" ] && proto_args=' in code, (
        "the pin must be applied precisely when the base is NOT overridden"
    )

    refused = subprocess.run(
        ["curl", "-sS", "--proto", "=https", "--proto-redir", "=https",
         "-o", os.devnull, "http://example.invalid/x"],
        capture_output=True, text=True,
    )
    assert refused.returncode != 0, (
        "curl with the pin must refuse an http URL outright; if this passes, the "
        "flags are not doing what the default path relies on them for"
    )


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root reads regardless of mode, so this arm cannot create the failure it clears",
)
def test_an_UNREADABLE_deploy_dir_on_the_box_is_CANNOT_LOOK(tmp_path) -> None:
    """THE WALK THAT GOES SILENT WITHOUT CHANGING THE ANSWER (Tesla, round 3).

    SKIPPED AS ROOT, deliberately. A mode-000 directory does not stop root, so
    under root this arm cannot create the failure it exists to clear — and an arm
    that cannot go red is not evidence, whichever way it lands. CI is
    `ubuntu-latest` with no container, so it runs as a normal user and the arm is
    live there; a future containerised CI would skip it loudly rather than
    reporting a pass it did not earn.

    `done < <(find ...)` discards the process substitution's status. If find
    fails — missing binary, unreadable directory — the stream is simply EMPTY:
    no files compared, nothing drifted, compose still matches, exit 0. A failed
    walk was indistinguishable from a clean one, which is this file's signature
    failure committed by the walk that looks for it.

    Forced with a mode-000 directory, which is the cheapest real way to make
    find fail. Mutation-checked: without the status check this returns CLEAN."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", BASELINE)
    deploy = box / "deploy"
    deploy.chmod(0o000)
    try:
        result = _run(box, tag)
    finally:
        deploy.chmod(0o755)  # so tmp_path cleanup can run

    assert result.returncode == CANNOT_LOOK, (
        "a walk that could not read deploy/ must not report a clean comparison: "
        + result.stdout
        + result.stderr
    )


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root reads regardless of mode, so this arm cannot create the failure it clears",
)
def test_an_UNREADABLE_box_file_is_CANNOT_LOOK_not_DRIFT(tmp_path) -> None:
    """A FOURTH STATE AGAIN (Carnot, cage-match round 5).

    A file the deploy user cannot read passes `-f`, fails `cmp`, and was
    reported as ordinary drift with an empty diff body — so update.sh told the
    operator to sync a file that is byte-identical, and the real fault
    (permissions) was never named. Measured before the fix: exit 1, "this box
    differs", no diff shown.

    I-could-not-read is a measurement failure, not an observation. The files
    here are IDENTICAL, so anything reported as a difference is a lie about the
    bytes as well as about the state."""
    box = _tree(tmp_path / "box", BASELINE)
    tag = _tree(tmp_path / "tag", BASELINE)
    victim = box / "deploy" / "update.sh"
    victim.chmod(0o000)
    try:
        result = _run(box, tag)
    finally:
        victim.chmod(0o644)

    assert result.returncode == CANNOT_LOOK, (
        "an unreadable file must not be reported as a difference — the bytes are "
        "identical: " + result.stdout + result.stderr
    )
    assert "READ" in result.stderr, result.stderr


def test_a_DANGLING_root_compose_symlink_is_refused_not_called_absent(tmp_path) -> None:
    """THE SAME SHAPE, THE SAME ANSWER, EVERYWHERE (Carnot, cage-match round 5).

    `-e` follows symlinks, so a dangling link reads as "not there". Under
    `deploy/` that was already handled and produced DRIFT; at a root compose path
    the bare `-e` remained, so the identical situation produced CLEAN with a
    warning. One script, two answers to one question.

    Every box-side presence test now routes through `_present`, which is what
    makes the answer uniform by construction rather than by remembering."""
    box = _tree(tmp_path / "box", BASELINE)
    (box / "docker-compose.build.yml").symlink_to(tmp_path / "no-such-target")
    tag = _tree(
        tmp_path / "tag",
        {**BASELINE, "docker-compose.build.yml": "services:\n  chat-island:\n    build: .\n"},
    )

    result = _run(box, tag)

    assert result.returncode == DRIFT, (
        "a compose fragment resolving to nothing is a broken artifact, not an "
        "absent one: " + result.stdout + result.stderr
    )
