#!/usr/bin/env python3
"""check-env-drift.py — does an island's LIVE .env match the repo's encrypted copy?

THE ONE GAP THIS CLOSES, and it is the whole scope. `preflight-compose-drift.sh`
already refuses a deploy when the box's `docker-compose.yml` or `deploy/` differ
from the tag being pulled. It cannot see `.env`, BY CONSTRUCTION: `.env` holds
per-box secrets and there is nothing public to compare it against. So half the
config surface — the half that held `APNS_VOIP_TOPIC` on 2026-09-11 while compose
failed to forward it — has never been checkable at all.

This makes it checkable. That is all it does.

WHAT IT DELIBERATELY DOES NOT DO, and why the list is this long. A design that
SHIPPED `.env` to the box went four rounds of `/design-temper` and never reached
SOUND (`docs/design/16-TEMPER.md`). Every finding across those rounds — the pin
that could not leave `.env`, the backup landing in a pruned generation, a stopped
tenant read as a missing one, an empty pin file silently meaning `edge`, a
half-applied discriminator that compared two names of the same live symlink — was
about WRITING, or about the generation directory that writing required. None of
them survive a read-only tool.

So: it never writes to the box. No generation, no symlink, no restore, no
baseline, no adoption, no cutover, and nothing new on the box to sync or prune.
It refuses and shows, exactly as ISL-0003's drift guard does, for ISL-0003's
stated reason: a box may legitimately carry local state, and silently clobbering
it is how #2301 became standing instead of caught.

NAMES ONLY, NEVER VALUES. `deploy/secrets/MANIFEST.txt` already established this
convention and its reasoning; this tool matches it. It reports WHICH KEYS differ,
never what they differ to. `--show-values` exists for an operator who has already
decided to look, and it is not the default.

NO TEMP FILE, NOTHING TO SHRED. The plaintext is read over a pipe from `sops -d`
and held only in this process. It never reaches a file, never reaches argv, and
never reaches the shell environment — which were the three exfil surfaces the v1
temper named (`/proc/<pid>/environ` is readable, `set -x` echoes, children
inherit, and `trap` does not run on SIGKILL). Removing the file removes the
shredding problem rather than solving it.

Usage:
    deploy/check-env-drift.py <island> [<island> ...] [--show-values]
    deploy/check-env-drift.py --all

Exit codes — three outcomes, and the third fails closed:
    0  MATCH         every key present on both sides with equal values
    1  DIFFERS       a real difference; the key names are on stdout
    2  COULD NOT RUN ssh failed, sops failed, no key, wrong box, parse failed

A check that could not run is not a check that passed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The two live islands. Kept here rather than in a conf file the tool would have
# to parse: there is no `deploy/islands/` in this repo, and inventing one to hold
# two rows would be the kind of machinery four temper rounds spent their time
# deleting. A third island adds a row.
ISLANDS = {
    "enspyr": {"ssh": "nick-mel", "path": "~/apps/aiko-chat-gateway"},
    "imagineering": {"ssh": "imagineering", "path": "~/apps/aiko-chat-gateway"},
}

EXIT_MATCH, EXIT_DIFFERS, EXIT_CANNOT_RUN = 0, 1, 2


class CannotRun(Exception):
    """Anything that stops the comparison happening. Never confused with a clean run."""


def parse_dotenv(text: str, where: str) -> dict[str, str]:
    """Parse with the SAME library the gateway parses with.

    `python-dotenv` is already a runtime dependency, and using it means "these
    two agree" means what it means at boot. Hand-rolling a parser here would
    reintroduce the risk it exists to measure — and `APNS_PRIVATE_KEY` is a PEM
    with REAL newlines inside quotes, which is exactly why SOPS's own dotenv
    parser rejects these files and they are stored as binary blobs.

    KNOWN LIMIT, stated rather than discovered: Compose's interpolation parser is
    not this parser. A value those two disagree about would read as MATCH here.
    That is a narrower gap than the one this closes, and it is not silently
    assumed away.
    """
    try:
        from dotenv import dotenv_values
    except ImportError as exc:  # pragma: no cover - environment problem, not logic
        raise CannotRun(
            "python-dotenv is not importable. Run from the repo venv: "
            f".venv/bin/python {Path(__file__).name} …"
        ) from exc

    import io

    parsed = dotenv_values(stream=io.StringIO(text))
    out = {k: v for k, v in parsed.items() if v is not None}
    if not out:
        raise CannotRun(f"parsed zero keys from {where} — refusing to call that a match")
    return out


def decrypted_repo_env(island: str) -> str:
    """`sops -d` over a pipe. Plaintext never touches a file, argv or the environment."""
    path = REPO / "deploy" / "secrets" / f"{island}.env.sops"
    if not path.is_file():
        raise CannotRun(f"no encrypted config at {path}")
    try:
        proc = subprocess.run(
            ["sops", "-d", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CannotRun("sops is not installed on this machine (control-side only)") from exc
    except subprocess.TimeoutExpired as exc:
        raise CannotRun("sops timed out — is it waiting on a key?") from exc
    if proc.returncode != 0:
        raise CannotRun(f"sops could not decrypt {path.name}: {proc.stderr.strip()[:400]}")
    return proc.stdout


def live_env(island: str) -> str:
    """Read the box's `.env`. READ ONLY — this is the only thing touching the box."""
    cfg = ISLANDS[island]
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", cfg["ssh"], f"cat {cfg['path']}/.env"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CannotRun(f"ssh to {cfg['ssh']} timed out") from exc
    if proc.returncode != 0:
        raise CannotRun(f"could not read {cfg['path']}/.env on {cfg['ssh']}: {proc.stderr.strip()[:400]}")
    return proc.stdout


def assert_same_island(island: str, repo: dict[str, str], live: dict[str, str]) -> None:
    """Confirm the box is the island we decrypted for, BEFORE reporting anything.

    Both boxes are shared, multi-tenant hosts, and a wrong-box comparison would
    report a wall of spurious differences that reads exactly like catastrophic
    drift. Cheap, and it is the read-only sibling of the tenant preflight the
    temper rounds demanded of the shipping design.
    """
    for key in ("ISLAND_ID", "ISLAND_DISPLAY_NAME"):
        a, b = repo.get(key), live.get(key)
        if a and b and a != b:
            raise CannotRun(
                f"WRONG BOX: {key} differs between the repo's {island} config and "
                f"{ISLANDS[island]['ssh']}. Refusing to compare."
            )


def compare(repo: dict[str, str], live: dict[str, str]) -> tuple[list[str], list[str], list[str]]:
    only_repo = sorted(set(repo) - set(live))
    only_live = sorted(set(live) - set(repo))
    changed = sorted(k for k in set(repo) & set(live) if repo[k] != live[k])
    return only_repo, only_live, changed


def report(island: str, only_repo, only_live, changed, repo, live, show_values: bool) -> None:
    ssh = ISLANDS[island]["ssh"]
    print(f"\n=== {island} ({ssh}) ===")
    if not (only_repo or only_live or changed):
        print("  MATCH — the box's .env agrees with the repo's encrypted copy.")
        return
    print("  DIFFERS")
    if only_repo:
        print(f"\n  in the REPO but NOT on the box ({len(only_repo)}):")
        for k in only_repo:
            print(f"    - {k}")
    if only_live:
        print(f"\n  on the BOX but NOT in the repo ({len(only_live)}):")
        for k in only_live:
            print(f"    + {k}")
    if changed:
        print(f"\n  present in both, VALUES DIFFER ({len(changed)}):")
        for k in changed:
            if show_values:
                print(f"    ~ {k}\n        repo: {repo[k]!r}\n        box:  {live[k]!r}")
            else:
                print(f"    ~ {k}")
    if changed and not show_values:
        print("\n  (key names only — pass --show-values to print the values themselves)")


def check(island: str, show_values: bool) -> int:
    repo = parse_dotenv(decrypted_repo_env(island), f"repo copy of {island}")
    live = parse_dotenv(live_env(island), f"live .env on {ISLANDS[island]['ssh']}")
    assert_same_island(island, repo, live)
    only_repo, only_live, changed = compare(repo, live)
    report(island, only_repo, only_live, changed, repo, live, show_values)
    return EXIT_DIFFERS if (only_repo or only_live or changed) else EXIT_MATCH


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Does an island's live .env match the repo's encrypted copy? Read-only.",
        epilog="Exit: 0 match, 1 differs, 2 could not run (fails closed).",
    )
    ap.add_argument("islands", nargs="*", choices=[*ISLANDS, []], help="island name(s)")
    ap.add_argument("--all", action="store_true", help="check every known island")
    ap.add_argument(
        "--show-values",
        action="store_true",
        help="print the differing values, not just their key names (default: names only)",
    )
    args = ap.parse_args()

    targets = list(ISLANDS) if args.all else args.islands
    if not targets:
        ap.error("name at least one island, or pass --all")

    worst = EXIT_MATCH
    for island in targets:
        try:
            worst = max(worst, check(island, args.show_values))
        except CannotRun as exc:
            print(f"\n=== {island} ===\n  COULD NOT RUN — {exc}", file=sys.stderr)
            worst = EXIT_CANNOT_RUN
    return worst


if __name__ == "__main__":
    sys.exit(main())
