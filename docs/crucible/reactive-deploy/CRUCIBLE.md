# Crucible — Reactive island deploy (islands react to a release)

*Movement 1 (Ore) — target pre-selected by Nick, consent gate crossed by explicit
direction. This file is the enthusiasm case + the falsifier the Temper must strike.*

## The pick

The two live islands should **react to a new container release** instead of a human
bumping `ISLAND_VERSION` and running `deploy/update.sh` by hand. A thin **watcher on
each box** (systemd timer) polls GHCR for a new image digest on the **release-tag
channel** (`v*` semver, not `edge`); on a digest change it runs the **existing
`update.sh`** (backup-DB-first fail-closed → `compose pull` → `up -d` → verify
`/health`), then **auto-rolls-back to the previous digest on health failure** and
**notifies** on both success and failure.

## Why this thrills me — AND what it changes

The heat: nearly every *piece* already exists and is battle-tested — the registry
publishes immutable digests, `update.sh` is the safe executor, the DB is an external
volume that survives restarts. The missing piece is a **~30-line trigger**, and adding
it turns "cut a tag, then remember to ssh into two boxes" into "cut a tag, walk away."
The *oh-of-course*: the release tag is **already** the intentional human gate — the
automation removes toil *after* the decision, not before it.

What it changes (impact, concrete): removes a recurring manual, error-prone step (the
last hand-`ssh`-to-prod in the pipeline — the same class that once zeroed enspyr's
compose to 0 bytes), and — if the safety design is right — makes deploys *safer* than
today by making auto-rollback structural instead of a human noticing `/health` is red.

## The falsifier (what would prove this ore is slag)

**If auto-rollback cannot be made correct and race-free on these boxes, the ore is
slag** — because an *unwatched* auto-deploy without a trustworthy rollback is strictly
worse than today's watched manual deploy. Specifically, this candidate dies if any of:
- rollback can't reliably restore the exact previous running digest (e.g. the previous
  image was GC'd, or compose can't pin a digest cleanly), so a bad release leaves prod
  down with no automatic recovery;
- the watcher-vs-manual-deploy and watcher-vs-watcher races can't be closed with a
  simple lock, so concurrent runs can corrupt the running stack or the backup;
- health "green" is not a trustworthy signal (a container that serves `/health` 200 but
  is actually broken), so rollback never fires when it should.

If those three hold (rollback correct, races locked, health trustworthy), the heat is
real and the plan is worth forging. The Temper must strike exactly here.

## Verified-real substrate (not invented)

- `.github/workflows/release.yml` — publishes `edge` (main) + `vX.Y.Z` (tags) to
  `ghcr.io/nickmeinhold/aiko-chat-island`, multi-arch, by digest.
- `deploy/update.sh` — backup-first fail-closed, `compose pull && up -d`, verify
  `/health`, docker-elevation autodetect.
- Two live islands: chat.imagineering.cc (nick/docker), chat.enspyr.co
  (ubuntu/`sudo -n docker`). DB = external named volume `aiko_data`.

## Scope boundary

Orthogonal to the parked #47 (declarative CONFIG/.env delivery, push-from-laptop via
SOPS). This is IMAGE-release reactivity (pull, box-side). They compose later; ship this
as its own increment.
