# Design 18: Operator opt-in auto-update (channel, restore-on-failure, compat guard)

**Status:** v3, CANDIDATE for `/design-temper` round 3, the LAST (≤ 3). Nothing is built.
Round 1 RECAST 4/4, round 2 RECAST 3/3 (Kelvin dark); see [`18-TEMPER.md`](18-TEMPER.md). The
bar still applies: **≤ ~1.5× v1, and every piece removes a failure mode.** v3's main fold is a
subtraction: the on-host rehearsal is gone.
**Answers:** Nick, 2026-08-16 and 2026-10-10: *"the option to have the server automatically
pull the latest image… it's what I want."*
**Decided (Nick, 2026-10-10):** a promoted channel by default, raw `:latest` opt-in. **enspyr-melb**
(`chat.enspyr.co`, where the phones ring) is the canary, and **enspyr-syd**
(`chat.imagineering.cc`) is the test island. So the island with real traffic takes every
release first. That's deliberate; don't "fix" it.

## The flaw this must answer

Rolling back the image doesn't roll back the database. An image that migrated forward and then
failed leaves the old image on a newer schema, and `migrate.py` serves anyway
(`MIGRATE_SKIP_UNKNOWN_REVISION`).

## The core move: record the start of serving, never rewind past it

The gateway writes `/data/.served`, holding the candidate's image digest (passed in as
`ISLAND_IMAGE` env) and its container id, **at the end of lifespan startup**: after
migration, immediately before the bus subscribe and the port bind (uvicorn binds after
lifespan). The watcher deletes any `.served` before cutover and reads it only after
`compose stop`, so `restart: always` can't race it. **Data is restored only if the
marker for this digest and container is absent.** Absent means nothing was served or
consumed, so the restore loses nothing by construction. Present means an image-only rollback,
which the compat guard admits or refuses. Registrar and chat mount no volume. Their off-box
effects during any stop are accepted, exactly as with a manual deploy today.

## 1. Compat guard (`migrate.py`): build first, needed regardless

- **Unknown revision ⇒ refuse** (`MIGRATE_REFUSE_UNKNOWN_REVISION`, non-zero exit). This
  reverses PR#116's serve-anyway, whose own comment says it was "safe ONLY if N-1…
  NOT yet enforced".
- `MIGRATE_ALLOW_UNKNOWN_REVISION=1` is for a human's deliberate manual rollback. The watcher
  never sets it, and the terminal-state runbook forbids it (§3).
- **No floors, no table.** Backward-compat floors come later as their own design, with CI
  that boots the old image and writes to every rebuilt table.
- **Limit:** the guard protects only rollbacks *to* guarded images. The watcher refuses to
  auto-restore to an image whose version label predates the guard.

## 2. Channels and promotion

- **`:latest`** (exists: semver releases only). Followed by enspyr-syd and enspyr-melb.
- **`:stable`** (new). Every other island's default. A CI job samples **hourly**, and each
  sample is green only if **both**:
  - **enspyr-syd:** `/health` 200, `build.ref` + `build.git_sha` match the release, and a
    **synthetic round trip** passes. A probe account logs in (password `/login`; the
    password is a CI secret), posts to a probe channel, and reads the message back over WS.
    The probe account is excluded from the island-mark activity counts.
  - **enspyr-melb:** `/health` 200 with `aiko_connected`, and the same ref + sha.
- **Memory, not a timestamp:** the streak `<digest>:<count>` lives in a repo variable. A
  red sample, a change in `:latest`'s digest, or a missing field resets it (fail closed).
  At 24 consecutive greens CI runs `imagetools create -t :stable <that digest>`.
  `PROMOTION_HOLD` vetoes; `PROMOTE_NOW=<digest>` is the hotfix override (a human's act,
  logged).
- **After promotion:** a red sample within 24 h stops further promotion and opens an issue.
  `:stable` is NOT moved back. A channel downgrade would make every follower roll back across
  migrations, which the guard would refuse. Islands that already pulled are protected only by
  their own watcher. **Named limit.**
- **Operator setting:** `AUTO_UPDATE=off` (**default**, today's behaviour; `update_nudge`
  still nags), `stable`, or `latest`.
- **Window:** the timer's `OnCalendar=`. Default `03:00..05:00` local; any other window is the
  operator's edit.

## 3. The watcher: systemd timer, shell, two phases (ISL-0003 holds)

Every run takes `flock -n` (shared with `update.sh`, same user; busy ⇒ exit). It resolves the
channel's index digest anonymously and compares it with the running one.

**Phase A: prepare, live stack untouched.** Pull by digest. Record the running image's id +
digest in `deploy/.auto-update/previous` (rollback re-pulls the digest if it was pruned). Run
`preflight-compose-drift.sh`. Require free disk ≥ 2× the db size. Check that every volume
in `compose config` is in the watcher's classified list (§ manifest). **Any failure retries
next poll and never quarantines.**

**Phase B: cutover.**
1. Renew the off-box lease (below). Stop gateway, registrar and chat. Delete `.served`.
   `.backup` into `deploy/.auto-update/pre-<digest>.db` (keep the last 2), then
   `integrity_check`.
2. Write `ISLAND_IMAGE=<registry>@<digest>` to `deploy/.auto-update/image.env`, the
   **watcher-owned second `--env-file`**, never the SOPS-rendered `.env` (#320's drift check
   compares that byte-for-byte). `up -d`.
3. **One budget, `AUTO_UPDATE_BUDGET` (default 600 s), covering migration and startup.**
   A migration still running is a wait, not a verdict. **Healthy** = all three containers
   running/healthy **and** `/health` 200 within the budget ⇒ record current, renew the lease,
   done.
4. **Otherwise (non-zero exit, budget spent, or any error after step 1):** `compose stop`, then
   read the marker.
   - **absent** ⇒ delete `aiko.db`, `-wal` and `-shm`, copy `pre-<digest>.db` in, start previous.
   - **present** ⇒ start previous on the current data, and the guard decides.
5. **Previous healthy** ⇒ quarantine the digest (only a newer release supersedes it); notify.
   **Previous unhealthy or refused** ⇒ **terminal**: stay stopped, write
   `deploy/.auto-update/state` = host-failed (auto-update suspended until cleared) plus
   diagnostics (phase, both digests, schema head before and after, marker contents, exit
   codes, `/health` bodies, guard refusal), and don't quarantine. **Runbook:** fix forward with
   a newer release, or restore `pre-<digest>.db` and *accept losing the writes since the
   marker's time* (the diagnostics show that window). **Never**
   `MIGRATE_ALLOW_UNKNOWN_REVISION` here.

**Paging, a lease rather than silence.** `AUTO_UPDATE` refuses to turn on without
`AUTO_UPDATE_PAGE_URL`, an off-box dead-man (a healthchecks-style service). The watcher pings
it on every run, and the ping's grace period is longer than the budget plus a rollback. A
planned cutover therefore renews the lease and never pages, while a dead or host-failed island
stops pinging and does page.

**State manifest.** `aiko_data` holds the db (restored as a set), plus the worker-guard lock and
`.served`, both disposable. `mosquitto_data` is not restored: the gateway uses a paho clean
session, so the broker holds no backlog for it. A volume missing from this list fails Phase A
until someone classifies it.

**Bootstrap, once per box, by hand:**
1. Take `ISLAND_VERSION` out of SOPS (ct#5165).
2. Sync the compose file whose aiko services use `image: ${ISLAND_IMAGE:?}` (this kills
   `:-edge`, design 16's empty-pin trap).
3. Have `update.sh` write `image.env`.
4. Set `AUTO_UPDATE_PAGE_URL`.
5. Only then set `AUTO_UPDATE`.

`preflight-compose-drift.sh` enforces step 2.

## Out of scope and named tradeoffs

- **Healthy-but-wrong:** the probe covers one round trip, and enspyr-melb's real use covers
  the tail. Owned by the canary's operator (Nick). A smoke *suite* is deliberately not built
  (that's the design-16 ratchet).
- **The canary takes every release first** (Nick's decision). **CI holds a probe-account
  password** for enspyr-syd: a user credential, not a deploy key.
- **Push-CD** is still rejected: the island pulls.

## Round 2 → v3

| # | Finding | v3 |
|---|---|---|
| 1 | Phase B relocated a failure (shared disk/mem, quarantine authority, bus-blind) | **Removed**: no on-host rehearsal; the test island + canary are the rehearsal |
| 2 | 120 s knife at cutover | One budget (600 s); migrating = waiting |
| 3 | Marker keyed by ref, written too early | Digest + container id, end of lifespan, cleared before cutover, read after stop |
| 4 | Self-reported one-shot latch, idle canary | CI streak memory, two islands, synthetic probe + real-use canary; no un-promote (named) |
| 5 | Pin collides with SOPS `.env` | Watcher-owned `image.env`; ct#5165 prerequisite |
| 6 | Terminal state has no recovery | Diagnostics + runbook; override forbidden |
| 7 | Dead-man rings on the living | Lease pinged every run |
| 8 | Bootstrap cliff, prose manifest | Five ordered steps; unclassified volume fails Phase A |
