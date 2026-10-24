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


def test_the_wrapper_passes_BOTH_env_files_and_strips_the_shell(tmp_path: Path) -> None:
    log = _fake_docker(tmp_path)
    root = _island(tmp_path)
    r = _run_wrapper(tmp_path, root, "ps", ISLAND_IMAGE="evil:1", ISLAND_VERSION="0.0.1")
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
    """The two measured facts the design rests on, re-measured against the real
    docker-compose.yml: bare refuses naming the wrapper; the wrapper resolves the
    pin even with a hostile export in the shell."""
    root = _island(tmp_path)
    for name in COMPOSE_FILES:
        shutil.copy2(REPO / name, root / name)
    (root / ".env").write_text("JWT_SECRET=" + "x" * 40 + "\nISLAND_VERSION=0.9.5\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ISLAND_")}

    bare = subprocess.run(["docker", "compose", "config", "--images"], cwd=root,
                          capture_output=True, text=True, env=env)
    assert bare.returncode != 0, "a bare call must not resolve any image"
    assert "deploy/compose.sh" in bare.stderr, bare.stderr

    wrapped = subprocess.run([str(root / "deploy" / "compose.sh"), "config", "--images"],
                             capture_output=True, text=True,
                             env={**env, "ISLAND_IMAGE": "evil:1"})
    assert wrapped.returncode == 0, wrapped.stderr
    assert _ref(wrapped.stdout).stdout == "0.17.1\n"
