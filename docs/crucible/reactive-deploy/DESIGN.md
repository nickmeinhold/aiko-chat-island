# Design: Reactive island deploy (islands react to a release)

*Movement 3 (Cast) — DRAFT frame; the digest/rollback/lock MECHANICS sections are
finalized against RESEARCH.md (Heat) before this goes to Temper. Frame (problem,
forks, blast-radius, claims-to-falsify, rejected alternatives) is research-independent.*

## Problem

Shipping an island update today is: merge → cut `vX.Y.Z` tag → CI publishes the
multi-arch image to GHCR → **a human bumps `ISLAND_VERSION` on each box and runs
`deploy/update.sh`**. That last hand-`ssh`-to-two-boxes step is the remaining manual,
error-prone link in the pipeline (the same class of over-ssh manual op that once zeroed
enspyr's compose to 0 bytes). We want the islands to **react** to a published release.

## The shape (chosen)

A **pull-based watcher** on each box (systemd `.timer` + `.service`), triggered by a
new image **digest on the release channel** (`v*` semver, not `edge`). On a digest
change it runs the **existing `deploy/update.sh`** (backup-DB-first fail-closed → pull →
`up -d` → verify `/health`), then — net-new — **auto-rolls-back to the previous digest
on `/health` failure**, and **notifies** (Telegram Bot API from the box) on success,
failure, and rollback.

Why pull, not push (CI-SSH): keeps prod SSH credentials OUT of CI (a compromised Action
must not get a prod shell — cage before monster), matches the existing no-inbound-surface
model (the box already pulls its image), and is literally what "islands react" means.

### Components
1. **`deploy/watch-release.sh`** — the watcher body: resolve the channel's current
   registry **index** digest credential-free (anon GHCR token dance + `HEAD` +
   `Accept: application/vnd.oci.image.index.v1+json`, read `Docker-Content-Digest`;
   RESEARCH §1), compare to the box's own **state file**
   `/var/lib/aiko-island/deployed.digest`, and if changed (and not frozen) invoke the
   deploy-with-rollback. Cross-checks the running container's `.RepoDigests[0]` as a
   sanity guard so a hand-run `update.sh` is still noticed (RESEARCH §2).
2. **Deploy-with-rollback (folded INTO `update.sh`, the one door)** — capture the
   running **index** digest before pulling, `docker tag …@<LASTGOOD> :lastgood` to pin
   it against GC, run the existing backup→pull→`up -d`, then gate on BOTH `/health` 200
   AND *running-digest == the digest we intended to deploy* (catches an `up -d` that
   silently kept the old container — no app change needed). On failure: generate a
   `rollback.override.yml` pinning `image: …:lastgood`, `up -d`, re-verify, notify. On
   success: write the new index digest to the state file. All THREE island services
   (gateway + registrar + ChatServer) share the one `…:${ISLAND_VERSION}` ref, so they
   move/roll-back as one unit (RESEARCH §3).
3. **systemd units** — `aiko-island-watch.timer` (every N min) + `.service`
   (`Type=oneshot`, `RuntimeDirectory=aiko-island`). systemd already coalesces
   timer×timer overlap; the human×timer collision is closed by an `flock -n` on fd 9
   over `/run/aiko-island/update.lock` **inside `update.sh`** (fail-closed, abort-not-
   queue), so every path — timer, manual, future caller — passes the same gate (RESEARCH §5).
4. **Freeze switch** — a sentinel file (`/etc/aiko-island/deploy-freeze` or a repo-path
   equivalent) the watcher checks first; present ⇒ skip (suppress auto-deploy during an
   incident). Fail-safe: if the watcher can't determine freeze state, it does NOT deploy.
5. **Notification** — `notify()` = `curl` to Telegram Bot API on success / failure /
   rollback; token+chat_id in `/etc/aiko-island/notify.env` (mode 0600, deploy-principal
   owned), loaded via systemd `EnvironmentFile=`. Never in the repo, never on a
   long-lived process's argv. `notify` swallows its own errors — a Telegram outage must
   never wedge a rollback (RESEARCH §8).

## Build order (core-first, each step independently useful)

1. **Manual `deploy/redeploy.sh <digest>` with rollback** (no automation yet) — the
   deploy-then-verify-then-rollback core, run by hand. *Independently useful:* a safe
   one-shot "deploy this digest, auto-restore if it breaks" even before any watcher.
2. **Digest-diff detector** (`watch-release.sh`, dry-run/notify-only) — resolve channel
   digest, compare to running, LOG/notify "would deploy" but do nothing. *Independently
   useful:* a drift monitor; proves the digest comparison before it can touch prod.
3. **Wire the watcher to the rollback deploy** behind the freeze switch — the reactive
   loop. *Independently useful:* the actual feature.
4. **systemd timer + lock + notifications** — schedule it, make it single-instance,
   wire success/failure/rollback alerts.
5. **Roll out to imagineering first (canary-by-hand), then enspyr** — one box proves it
   live before the second. *Independently useful:* staged confidence.

## Blast-radius & consent spine (cage before monster)

- **Owner:** Nick (the two live islands). **Injection surface:** the registry digest
  (an attacker who could push to GHCR could drive a deploy — but that requires
  compromising the repo's publish path, which is already the trust root of the image;
  the watcher adds no NEW push surface, only reacts to the SAME digests a human would).
- **The safety spine is backup + health + rollback**, all fail-closed: no deploy without
  a good DB backup (update.sh already aborts if backup fails); no "success" without
  `/health`; a failed health auto-restores the previous digest. The freeze switch is the
  human override.
- **Not a webhook** (no inbound endpoint on the box to attack). **No prod creds in CI.**
- Code PR gets a `/cage-match` by law (infra-as-code, whole-device blast radius,
  state-lifecycle) — separate from this design temper.

## Claims to falsify (strike these at Temper)

1. **Auto-rollback is correct and reliable** — it restores the EXACT previous running
   digest, the rollback target can't be GC'd before it's needed, and if rollback ITSELF
   fails the box ends in a named, alerted state (not silently down). *If false, the ore
   is slag* (per CRUCIBLE.md falsifier).
2. **The digest comparison is race-free and correct** — the "has the channel changed?"
   check compares the right digests (registry index vs locally-resolved manifest is a
   known footgun), and concurrent runs (watcher×watcher, watcher×manual `update.sh`) are
   serialized by a lock that can't deadlock or go stale.
3. **`/health` is a trustworthy enough deploy gate** — a 200 means the release is
   actually serving (the entrypoint migrates fail-closed, so a broken migration DOES
   fail health), OR we name the residual (a container that 200s but is subtly wrong) and
   accept it with a cheap upgrade path.
4. **Reacting to `v*` (latest semver) is the right trigger** vs a promoted `stable`
   pointer — and both boxes reacting near-simultaneously (no cross-box canary) is
   acceptable given correct per-box rollback.
5. **A poll interval (minutes) is acceptable latency** for "reacting" to a release.

## Rejected alternatives (what simpler/other shape was passed over, and why)

- **Watchtower (off-the-shelf):** does its own pull+recreate, bypassing the backup-first
  + health-verify + rollback spine on a data-holding box. Rejected: the safety spine is
  the whole point; reusing `update.sh` preserves it.
- **Push CD from GitHub Actions (SSH to boxes on release):** gives real canary
  sequencing but puts prod SSH creds in CI (attack surface). Rejected for v1 on
  blast-radius grounds; revisit if cross-box canary becomes necessary.
- **Registry webhook → box listener:** an inbound endpoint on the box is new attack
  surface (cage before monster) and GHCR webhook support is weak. Rejected.
- **Fold into #47 (declarative config):** orthogonal concern (config push vs image
  pull); bundling would delay both. Kept separate; they compose later.

## Fold (author self-pass — degenerate states + corrections folded back)

Struck my own casting before the cross-family strike. Six degenerate states; the first
two are load-bearing and change the design:

1. **Failed-release redeploy THRASH (the worst one, folded in).** A bad release
   deploys → health fails → rolls back to `:lastgood`. But the state file still holds the
   OLD good digest and the registry channel still points at the BAD digest — so the NEXT
   poll sees "changed" again and **redeploys the known-bad release, every interval,
   forever.** Fix (folded): on a failed deploy+rollback, write the failed index digest to
   `/var/lib/aiko-island/failed.digest`; the watcher SKIPS a channel digest that equals
   the last-failed one (until a NEWER digest appears or a human clears it). A bad release
   is thus attempted exactly ONCE per box, then quarantined + alerted — not thrashed.
2. **First-run / missing state file (folded in).** With no `deployed.digest`, "registry
   vs missing file" reads as "changed" → an unwanted deploy on install even if the box is
   already on that digest. Fix (folded): the installer SEEDS `deployed.digest` from the
   currently-running container's index digest; the watcher treats a missing state file as
   **seed-and-skip**, never deploy. Deploys fire only on a genuine CHANGE from a known baseline.
3. **Registry unreachable (network blip) = NO-OP.** The watcher acts only on a
   *successfully resolved* digest that differs; a failed token/HEAD is logged and skipped,
   never interpreted as a change. Fail-safe by construction.
4. **Any `update.sh` non-zero exit → notify.** Backup-fail (aborts pre-stack), health-fail,
   rollback-fail all surface a Telegram alert — an auto-deploy must never fail silently.
5. **Rollback-of-rollback failure = terminal alerted state.** `:lastgood` tag +
   no-`prune -a`-on-timer is the mitigation; if rollback still fails, page a human and
   leave the box as-is (don't thrash). Named residual.
6. **Both islands react to one bad release near-simultaneously** (no cross-box canary):
   each deploys, health-fails, rolls back, quarantines independently — so a bad release
   breaks both for ~one deploy+rollback window (tens of seconds each), then both
   self-heal. Accepted for v1 because correct per-box rollback bounds the blast; the
   `stable`-pointer channel (below) is the canary UPGRADE path (promote to a
   `stable-confirmed` pointer enspyr watches only after imagineering succeeds).

Channel decision resolved (was an open variable): **watch a moving `stable` tag**, not
latest-`v*`-semver. `stable` is simpler to poll (one fixed tag, no semver sort), and it
*decouples* "publish a version" from "release it to islands" — the seam a canary later
plugs into. v1: `release.yml` sets `stable` → the new `vX.Y.Z` on every release tag, so
**cutting the tag IS the gate and the islands react** (matches the chosen trigger). The
publish-without-release / canary upgrade = make `stable`-promotion a separate manual/
dispatch step later, no watcher change.

## Open variables

*Resolved in Fold:* channel = moving `stable` tag (release.yml sets it on `v*`); canary
= none in v1 (per-box rollback is the safety), `stable`-pointer is the upgrade seam;
redeploy-thrash = `failed.digest` quarantine; first-run = seed-and-skip.

*Still open (Temper / Nick to weigh):*
- **Poll interval:** default 2–5 min. (Latency vs registry chattiness.)
- **Who sets `stable`:** auto-on-`v*`-tag (v1, one gate) vs a separate promote step
  (enables publish-without-release + canary). *Leaning auto-on-tag for v1.*
- **Health depth for v1:** `/health` 200 + running-digest assert (chosen) vs also adding
  a readiness `SELECT 1` now. *Leaning defer readiness to a follow-up.*
- **Mechanics (RESOLVED against RESEARCH.md):** digest resolution = anon curl HEAD +
  index `Accept` → `Docker-Content-Digest`; comparison = watcher-owned state file of the
  **index** digest (never `.Id`, never index-vs-per-arch — the redeploy-loop footgun);
  rollback re-pin = generated `@sha256:` override file; GC-protection = `:lastgood` tag +
  the no-`prune -a`-on-a-timer invariant; lock = `flock -n` in `update.sh`.

## Must-verify on the LIVE boxes before build (RESEARCH open questions — ground, don't assume)

These are the premises the rollback/lock guarantees rest on; falsify each on both boxes
read-only before building (the design is void if any is wrong and unaddressed):
1. **No periodic `docker … prune -a`** on either box (cron/timer/janitor) — if one
   exists it can reap the `:lastgood` rollback target. Invariant: no untimed `-a` prune;
   if one exists, exclude `:lastgood` and move it outside the deploy window.
2. **The timer's `User=` == the principal a human uses** for `update.sh` (esp. the
   `sudo -n docker` / `ubuntu` box) — else the `flock` path isn't shared and protects
   nothing.
3. **Image store type** (containerd vs classic graphdriver) — `.RepoDigests[0]` carries
   the index digest when pulled by tag on both, but confirm on the real box; the
   state-file approach makes this moot for the loop, but the cross-check relies on it.

## Health gate — necessary-but-not-sufficient (named residual)

`/health` 200 proves liveness (and, because the entrypoint migrates fail-closed, a
broken migration DOES fail it). v1 gates on `/health` 200 **AND** running-digest ==
intended-digest (closes stale-container). Residual: a container that 200s but is subtly
degraded (DB attached but a logic regression) still passes — accepted for v1. Cheap
upgrade path (follow-up, not v1): `/health` does a DB `SELECT 1` (readiness), or the app
self-reports its build digest. Named, owner: design; mirrors podman auto-update's lesson
that "started" ≠ "healthy" (RESEARCH §6–7).
