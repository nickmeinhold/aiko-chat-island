"""The image pin lives in image.env, and every compose call goes through one door
(claude-tasks#5165; design 18 bootstrap step 1).

Three properties, each driven rather than read:

1. A MISSING PIN REFUSES. Before #5165 the compose template was
   `…:${ISLAND_VERSION:-edge}`, so a pin that went missing quietly became "track
   main". Now `${ISLAND_IMAGE:?}`: measured on enspyr-syd 2026-10-24, a bare
   `docker compose ps` exits 1 naming the wrapper.
2. ONE DOOR. island_compose pins the file set and BOTH env files, and strips the
   shell's ISLAND_IMAGE / ISLAND_VERSION, because the shell beats every env file
   in Compose's precedence (measured, PR#190 round 5).
3. THE DRIFT GUARD'S REF IS COMPOSE'S OWN RESOLUTION. island_ref_from_images reads
   `config --images` and refuses anything it cannot turn into exactly one tag.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "deploy" / "lib" / "island-compose.sh"
WRAPPER = REPO / "deploy" / "compose.sh"
OURS = "ghcr.io/nickmeinhold/aiko-chat-island"
COMPOSE_FILES = ("docker-compose.yml", "docker-compose.build.yml")


def _ref(images: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"; island_ref_from_images'],
        input=images, capture_output=True, text=True)


# --------------------------------------------------------------- the ref parser

def test_a_tag_pin_yields_that_tag() -> None:
    r = _ref(f"{OURS}:0.17.1\n{OURS}:0.17.1\neclipse-mosquitto:2\n{OURS}:0.17.1\n")
    assert r.returncode == 0, r.stderr
    assert r.stdout == "0.17.1\n"


@pytest.mark.parametrize("images, why", [
    ("eclipse-mosquitto:2\n", "none of our services resolved"),
    (f"{OURS}:0.17.1@sha256:{'0' * 64}\n", "tag-plus-digest is a digest too (Noether r2)"),
    (f"{OURS}:\n", "an empty tag is a bad pin, not an empty ref (Noether r3)"),
    (f"{OURS}:1\n{OURS}:2\n", "the services disagree; they must share one image"),
    (f"{OURS}@sha256:{'0' * 64}\n", "a digest names no git tag to compare against"),
    ("ghcrXio/nickmeinhold/aiko-chat-island:1\n", "a lookalike repo: the dots are literal"),
    (f"ghcr.io/someone-else/aiko-chat-island:1\n", "a fork's image is not our tag"),
    ("", "compose printed nothing"),
])
def test_anything_but_exactly_one_tag_REFUSES(images: str, why: str) -> None:
    r = _ref(images)
    assert r.returncode == 1, f"must refuse: {why}; got {r.stdout!r}"
    assert r.stdout == "", "a refusal must not also print a ref someone could use"
    assert r.stderr.strip(), "a refusal must say why"


# ------------------------------------------------------------ the one door

def _fake_docker(tmp_path: Path) -> Path:
    """A `docker` that records its argv and the ISLAND_* it can see, then succeeds."""
    log = tmp_path / "argv.log"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{log}"\n'
        f'printf "IMG=%s VER=%s\\n" "${{ISLAND_IMAGE-unset}}" "${{ISLAND_VERSION-unset}}" >> "{log}"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return log


def _island(tmp_path: Path, *, image_env: bool = True) -> Path:
    root = tmp_path / "island"
    (root / "deploy" / "lib").mkdir(parents=True)
    shutil.copy2(WRAPPER, root / "deploy" / "compose.sh")
    shutil.copy2(LIB, root / "deploy" / "lib" / "island-compose.sh")
    (root / ".env").write_text("ISLAND_VERSION=0.9.5\n")
    if image_env:
        (root / "image.env").write_text(f"ISLAND_IMAGE={OURS}:0.17.1\n")
    return root


def _run_wrapper(tmp_path: Path, root: Path, *args: str, **extra_env: str):
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", **extra_env}
    return subprocess.run([str(root / "deploy" / "compose.sh"), *args],
                          capture_output=True, text=True, env=env)


def _lib(tmp_path: Path, root: Path, script: str, **extra_env: str):
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", **extra_env}
    return subprocess.run(["bash", "-c", f'ISLAND_ROOT="{root}"; DOCKER=docker; . "{LIB}"; {script}'],
                          capture_output=True, text=True, env=env)


def test_the_wrapper_passes_BOTH_env_files_and_strips_the_shell(tmp_path: Path) -> None:
    """island_compose itself, below every door's refusal: even if an export got past a
    door, it must not reach compose."""
    log = _fake_docker(tmp_path)
    root = _island(tmp_path)
    r = _lib(tmp_path, root, "island_compose ps", ISLAND_IMAGE="evil:1", ISLAND_VERSION="0.0.1")
    assert r.returncode == 0, r.stderr
    argv, seen = log.read_text().rsplit("IMG=", 1)
    assert argv.split("\n")[:-1] == [
        "compose", "-f", f"{root}/docker-compose.yml",
        "--env-file", f"{root}/.env", "--env-file", f"{root}/image.env", "ps",
    ], "image.env must come LAST: of repeated --env-files, the last one wins"
    assert seen.strip() == "unset VER=unset", (
        "the shell beats every env file in Compose's precedence, so an exported pin "
        "must never reach compose")


def test_the_build_overlay_is_an_option_not_a_second_door(tmp_path: Path) -> None:
    log = _fake_docker(tmp_path)
    root = _island(tmp_path)
    r = _run_wrapper(tmp_path, root, "--build-overlay", "up", "-d", "--build")
    assert r.returncode == 0, r.stderr
    argv = log.read_text().split("\n")
    assert argv[:5] == ["compose", "-f", f"{root}/docker-compose.yml",
                        "-f", f"{root}/docker-compose.build.yml"]
    assert "--env-file" in argv and argv[argv.index("up") - 1] == f"{root}/image.env"


def test_no_image_env_refuses_BEFORE_docker_runs(tmp_path: Path) -> None:
    log = _fake_docker(tmp_path)
    root = _island(tmp_path, image_env=False)
    r = _run_wrapper(tmp_path, root, "up", "-d")
    assert r.returncode != 0
    assert not log.exists(), "docker must not have been invoked at all"
    assert "printf 'ISLAND_IMAGE=" in r.stderr, "the refusal must give the fix"
    assert "still carries an ISLAND_VERSION line" in r.stderr, (
        "a pre-#5165 box must be told its old pin is no longer read")


# ------------------------------------------------- the compose files themselves

@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_every_island_image_is_REQUIRED_never_defaulted(name: str) -> None:
    lines = [ln.strip() for ln in (REPO / name).read_text().splitlines()
             if ln.strip().startswith("image:")]
    ours = [ln for ln in lines if "mosquitto" not in ln]
    assert ours, f"{name} has no island image line; this test stopped measuring"
    for ln in ours:
        assert ln.startswith('image: "${ISLAND_IMAGE:?'), (
            f"{name}: {ln!r} — a missing pin must refuse, not resolve to a default")


needs_compose = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode,
    reason="needs the docker compose CLI (config only; no daemon)")


@needs_compose
def test_REAL_compose_bare_call_refuses_and_the_wrapper_resolves(tmp_path: Path) -> None:
    """The measured facts the design rests on, re-measured against the real
    docker-compose.yml: with NO ISLAND_IMAGE in its environment a bare call refuses,
    naming the wrapper; the wrapper resolves image.env's pin. (A bare call WITH an
    exported ISLAND_IMAGE resolves the export; that is Compose's precedence and is
    stated as a limit in deploy/lib/island-compose.sh, Tesla PR#401 r2.)"""
    root = _island(tmp_path)
    for name in COMPOSE_FILES:
        shutil.copy2(REPO / name, root / name)
    (root / ".env").write_text("JWT_SECRET=" + "x" * 40 + "\nISLAND_VERSION=0.9.5\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ISLAND_")}

    bare = subprocess.run(["docker", "compose", "config", "--images"], cwd=root,
                          capture_output=True, text=True, env=env)
    assert bare.returncode != 0, "a bare call must not resolve any image"
    assert "deploy/compose.sh" in bare.stderr, bare.stderr

    # Through the lib with DOCKER=docker, not deploy/compose.sh: the wrapper's
    # island_docker_detect needs a DAEMON (`docker ps`), and `config` does not. Driving
    # the door here made a stopped daemon read as a pin regression (Carnot + Noether r3).
    wrapped = subprocess.run(
        ["bash", "-c", f'ISLAND_ROOT="{root}"; DOCKER=docker; . "{LIB}"; island_compose config --images'],
        capture_output=True, text=True, env=env)
    assert wrapped.returncode == 0, wrapped.stderr
    assert _ref(wrapped.stdout).stdout == "0.17.1\n"


# ------------------------------------------------------- round-1 cage-match fixes

def test_ONE_PIN_PER_RUN_island_compose_reads_the_snapshot(tmp_path: Path) -> None:
    """Carnot, PR#401 r1+r2: each compose call re-read the live image.env, so an edit
    mid-run (or design 18's watcher) made the guard certify one release while the script
    deployed another. island_pin_begin snapshots it once; every call reads the snapshot,
    and a later edit to image.env does not reach the run."""
    log = _fake_docker(tmp_path)
    root = _island(tmp_path)
    r = _lib(tmp_path, root,
             f'island_pin_begin; printf "ISLAND_IMAGE={OURS}:9.9.9\\n" > "{root}/image.env"; '
             'island_compose ps; cat "$_island_pin_file"; '
             'stat -f %Lp "$_island_pin_file" 2>/dev/null || stat -c %a "$_island_pin_file"; '
             'rm -f "$_island_pin_file"')
    assert r.returncode == 0, r.stderr
    argv = log.read_text().split("\n")
    snap = argv[argv.index("ps") - 1]
    assert snap != f"{root}/image.env", "the run must read the snapshot, not the live file"
    assert f"{OURS}:0.17.1" in r.stdout and "9.9.9" not in r.stdout
    assert r.stdout.strip().endswith("600"), "the snapshot must be owner-only"


def test_an_AMBIENT_pin_file_selector_is_ignored(tmp_path: Path) -> None:
    """Noether, PR#401 r2: the pin file's path must not come from the environment."""
    log = _fake_docker(tmp_path)
    root = _island(tmp_path)
    other = tmp_path / "other.env"
    other.write_text("ISLAND_IMAGE=evil:1\n")
    r = _lib(tmp_path, root, "island_compose ps", _island_pin_file=str(other))
    assert r.returncode == 0, r.stderr
    argv = log.read_text().split("\n")
    assert argv[argv.index("ps") - 1] == f"{root}/image.env"


@pytest.mark.parametrize("what, mutate, needle", [
    ("an exported ISLAND_IMAGE", {"env": {"ISLAND_IMAGE": f"{OURS}:0.18.0"}}, "ISLAND_IMAGE is set"),
    ("an exported ISLAND_VERSION", {"env": {"ISLAND_VERSION": "0.9.5"}}, "ISLAND_VERSION is set"),
    ("another key in image.env", {"image": f"ISLAND_IMAGE={OURS}:0.17.1\nJWT_SECRET=x\n"}, "ONLY the ISLAND_IMAGE line"),
    ("two pins in image.env", {"image": f"ISLAND_IMAGE={OURS}:1\nISLAND_IMAGE={OURS}:2\n"}, "exactly once"),
    ("an empty image.env", {"image": "# nothing\n"}, "exactly once"),
    ("an ISLAND_IMAGE line in .env", {"dotenv": f"ISLAND_IMAGE={OURS}:0.18.0\n"}, "carries an ISLAND_IMAGE line"),
    ("interpolation in image.env", {"image": "ISLAND_IMAGE=" + OURS + ":${RELEASE_TAG}\n"}, "literal image reference"),
])
def test_the_door_REFUSES_every_other_source_of_the_pin(tmp_path: Path, what, mutate, needle) -> None:
    """The class from PR#401 r2, enumerated: anything but image.env's one line steering
    the image refuses at the door, before docker runs."""
    log = _fake_docker(tmp_path)
    root = _island(tmp_path)
    if "image" in mutate:
        (root / "image.env").write_text(mutate["image"])
    if "dotenv" in mutate:
        with (root / ".env").open("a") as f:
            f.write(mutate["dotenv"])
    r = _run_wrapper(tmp_path, root, "up", "-d", **mutate.get("env", {}))
    assert r.returncode != 0, f"{what} must refuse"
    assert needle in r.stderr, r.stderr
    assert not log.exists(), "docker must not have been invoked"


def test_a_well_formed_image_env_with_comments_and_export_is_accepted(tmp_path: Path) -> None:
    log = _fake_docker(tmp_path)
    root = _island(tmp_path)
    (root / "image.env").write_text(f"# pinned 2026-10-24\n\nexport ISLAND_IMAGE={OURS}:0.17.1\n")
    r = _run_wrapper(tmp_path, root, "ps")
    assert r.returncode == 0, r.stderr


def _code(path: Path) -> str:
    return "\n".join(ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#"))


UPDATE = REPO / "deploy" / "update.sh"
STANDUP = REPO / "deploy" / "standup.sh"


@pytest.mark.parametrize("door, first_compose", [
    (UPDATE, "$COMPOSE config --quiet"),
    (STANDUP, "island_compose --build-overlay up -d --build"),
])
def test_every_multi_call_door_checks_and_snapshots_BEFORE_its_first_compose_call(door, first_compose) -> None:
    """Carnot r2: the r1 snapshot covered update.sh and missed standup.sh."""
    code = _code(door)
    req = code.index("island_require_pin ||")
    snap = code.index("island_pin_begin ||")
    assert req < snap < code.index(first_compose)


def test_the_drift_refusal_carries_the_rollback_exception() -> None:
    """Maxwell r2: the runbook said "do not sync when rolling back below v0.17.2" while
    the refusal the operator actually reads said "sync the named files"."""
    code = UPDATE.read_text()
    arm = code[code.index('1) die "this box\'s deploy tree differs'):]
    arm = arm[: arm.index(";;")]
    assert "BELOW v0.17.2" in arm and "Do NOT sync" in arm


def test_update_sh_resolves_config_LOUDLY_before_the_swallowed_running_check() -> None:
    """Tesla, PR#401 r1: the running check discards stderr, so a readable image.env with
    no usable ISLAND_IMAGE was reported as "isn't running, use standup.sh"."""
    code = _code(UPDATE)
    loud = code.index("$COMPOSE config --quiet ||")
    quiet = code.index("$COMPOSE ps --status running --services 2>/dev/null")
    assert loud < quiet


def test_update_sh_names_a_partial_sync_before_sourcing_the_lib() -> None:
    code = _code(UPDATE)
    guard = code.index('[ -f "$SCRIPT_DIR/lib/island-compose.sh" ] || die')
    source = code.index('. "$SCRIPT_DIR/lib/island-compose.sh"')
    assert guard < source


def test_standup_writes_the_new_pin_BEFORE_replacing_the_old_home() -> None:
    """Tesla, PR#401 r1: renaming .env first left a window where an abort destroyed
    ISLAND_VERSION before image.env existed; the next run then pinned `edge`."""
    code = _code(STANDUP)
    assert code.index('mv "$IMAGE_TMP" "$IMAGE_ENV"') < code.index('mv "$ENV_TMP" "$ENV_FILE"')


@pytest.mark.parametrize("args", [["--env-file", "other.env", "up"], ["-f", "x.yml", "up"], ["--project-directory", "/", "ps"]])
def test_the_human_door_REFUSES_global_options_that_would_outrank_the_pin(tmp_path: Path, args) -> None:
    """Tesla, PR#401 r3: a raw `--env-file other.env` lands after the pinned ones and wins."""
    log = _fake_docker(tmp_path)
    root = _island(tmp_path)
    r = _run_wrapper(tmp_path, root, *args)
    assert r.returncode != 0 and "global option" in r.stderr, r.stderr
    assert not log.exists()


def test_standup_refuses_an_exported_pin_BEFORE_writing_image_env() -> None:
    """Tesla, PR#401 r3: checked after the write, the failed run left `edge` behind."""
    code = _code(STANDUP)
    assert code.index("island_refuse_ambient_pin ||") < code.index('mv "$IMAGE_TMP" "$IMAGE_ENV"')


def test_update_sh_resolves_symlinks_before_locating_its_lib() -> None:
    code = _code(UPDATE)
    assert code.index('while [ -L "$_src" ]; do') < code.index('SCRIPT_DIR="$(cd -P "$(dirname "$_src")" && pwd)"')
