# Design 18: Operator opt-in auto-update (channel, restore-on-failure, compat guard)

**Status:** v2, CANDIDATE for `/design-temper` round 2 (of ≤ 3). Nothing is built.
v1 was RECAST 4/4 (zero DISSOLVE); [`18-TEMPER.md`](18-TEMPER.md) holds the nine findings
and the round-2 bar: **≤ ~1.5× v1, and every new piece removes a failure mode.**
**Answers:** Nick, 2026-08-16 and 2026-10-10: *"each island operator should have the
option to have the server automatically pull the latest image… it's what I want."*
**Channel policy (finding 4):** Nick, 2026-10-10, option (b): a promoted channel is the
default; raw `:latest` is an explicit opt-in.

## The flaw this must answer

Rolling back the **image** does not roll back the **database**. A new image migrates
the volume forward, fails, and the old image restarted on the newer schema either
crash-loops or corrupts. Today `migrate.py` makes corruption the default: on an unknown
revision it **serves anyway** (`MIGRATE_SKIP_UNKNOWN_REVISION`).

## The move that shrinks v2: record admission, don't gate it

v1's restore was justified by *"the new version never passed `/health`, so it never
served"*. All four families showed that's false: uvicorn serves, and the gateway's
lifespan joins the bus, before any health check sees it. The temper's fix was a HELD
boot mode (migrate, then answer only `/health` until the watcher admits it). That is
new code on every route, plus a chaos test to prove the gate holds.

v2 doesn't build a gate. It **records the moment the gate would have opened** and
**never rewinds data past it**:

- `entrypoint.sh` writes `/data/.served-<build ref>` **after** `migrate` succeeds and
  **before** `exec uvicorn`. One `touch`. From that line on, this generation may have
  accepted a write or consumed the bus.
- The watcher's rule: **data is restored only if the marker for the candidate is
  absent.** Absent means no process of this generation got past migration, so nothing
  was served, and restoring loses nothing *by construction*, not by a timing argument.
- With the marker present, the watcher may roll back the **image** only. The compat
  guard (§1) then decides whether that is safe, and refuses loudly if it isn't.

This removes finding 1 instead of guarding it: there is no admission window to
test, because no rewind ever crosses one. Kelvin's chaos test has nothing left to prove.
The cost is honest: a candidate that dies *after* starting uvicorn on a release that
migrated cannot be auto-healed, so it stops and pages (§3, terminal state). Rehearsal
(phase B) exists to make that case rare.

## 1. Compat guard in `migrate.py` (buildable now, needed regardless)

- **Unknown revision ⇒ refuse**, exit non-zero with `MIGRATE_REFUSE_UNKNOWN_REVISION`.
  This reverses PR#116's serve-anyway, on purpose. That tolerance existed so a manual
  image rollback onto a forward-migrated volume wouldn't crash-loop. It was "safe ONLY
  if N-1 compatible… NOT yet enforced", and the enforcement never came.
- **Override for a deliberate manual rollback:** `MIGRATE_ALLOW_UNKNOWN_REVISION=1`
  restores today's behaviour, with today's warning. The watcher **never** sets it.
- **Declared backward floors are deferred.** v1 proposed a `schema_compat` table with
  per-revision `compat_floor`s so compatible migrations could still allow an image-only
  rollback. The temper showed the floor is a graph walk, not a max (finding 5), and is
  only trustworthy once CI has booted the oldest admitted image and *written* to every
  rebuilt table. Every v1 floor would have been the revision itself anyway, and
  "floor = self everywhere" is exactly "unknown ⇒ refuse". So v2 ships the guard with
  **no table, no floors, no graph**. Finding 5 goes away until backward floors are wanted,
  and they come back as their own design together with the CI proof they need.
- **Named limit (finding 9):** the guard protects only rollbacks *to* images that
  contain it. The watcher reads the target image's `org.opencontainers.image.version`
  label and refuses to auto-restore to anything older than the first guarded release.

## 2. Channels: `:latest` is the canary's, `:stable` is everyone else's

- `release.yml` already publishes `:latest` (semver releases only, never `main`).
- **New: `:stable`**, advanced by a scheduled CI job (hourly) that reads the canary's
  **public** `/health`. It promotes release `vX` when **all** of these hold:
  1. `build.ref == vX`, `vX` is the newest release, and `/health` returns 200 with
     `aiko_connected: true`;
  2. **new field** `started_at` (process start, added to `/health`) is ≥ `SOAK_HOURS`
     (default 24) ago. A restart resets the soak, which is the conservative direction;
  3. repo variable `PROMOTION_HOLD` is unset (Nick's manual veto for a
     healthy-but-wrong release spotted by eye).
  Promotion is `docker buildx imagetools create -t :stable <vX index digest>`, which CI
  can already do. **No box holds registry credentials**; CI reads a public URL.
- **Canary quarantine withholds promotion for free:** a canary that rolled `vX` back
  reports `ref ≠ vX`, so condition 1 fails. There's no second signal to build.
- **Operator setting:** `AUTO_UPDATE=off` (**default**; today's behaviour, and
  `update_nudge` still nags), `stable`, or `latest`. Only the canary should run
  `latest`; third-party operators who would rather not depend on Nick's canary may
  choose it knowingly.
- **Window (Kelvin):** the systemd timer's own `OnCalendar=` *is* the operator's window.
  It needs no new setting. Default: every 15 min; Nick's enspyr might use `03:00..05:00`.
- The old `AUTO_UPDATE_DELAY_HOURS` clock is **deleted**: the soak now lives in promotion,
  where a failure on the canary can cancel it.

## 3. The watcher: systemd timer, shell, three phases (ISL-0003 holds)

Every run takes `flock -n` on a file in the deploy dir, shared with manual `update.sh`
under the same user; busy ⇒ exit, never queue. It resolves the channel's **index
digest** anonymously (GHCR is public) and compares it with the running one.

**Phase A: prepare, everything still up.** Pull the candidate **by digest**; pin the
running image by **id and digest** (record both in `deploy/.auto-update/previous`; if the
local image was pruned, rollback re-pulls the digest); run `preflight-compose-drift.sh`.
Any failure here (GHCR 5xx, disk, drift) **retries next poll and never quarantines**.

**Phase B: rehearsal, everything still up.** Python's `sqlite3.Connection.backup` of the
live db into a scratch volume (online and consistent, and it carries no WAL). Run the
candidate against that copy with `--network none`: migrate, start, require `/health` 200
from inside the container within 300 s. A slow table rebuild gets its time here, off
the outage clock. **Rehearsal failure ⇒ quarantine the digest** (it failed on a copy of
real data, so the release is at fault, not the host), page, and leave prod untouched.

**Phase C: cutover, only if B passed.**
1. Stop gateway, registrar, chat. Back up the db with `.backup` into
   `deploy/.auto-update/pre-<digest>.db` and check `integrity_check`.
2. Set `ISLAND_IMAGE=<registry>@<candidate digest>` and `up -d`.
3. **Healthy** = all three containers running/healthy **and** gateway `/health` 200
   within 120 s (finding 8). Record the digest as current. Done.
4. **Unhealthy, or any exit after step 1:**
   - **marker absent** ⇒ restore: delete `aiko.db`, `aiko.db-wal` and `aiko.db-shm`,
     copy the `.backup` file in as `aiko.db` (a backup carries no WAL, so this is the
     whole set; finding 2), start previous;
   - **marker present** ⇒ start previous on the current data. The guard lets it
     serve if the release didn't migrate, and refuses if it did.
5. **Previous healthy** ⇒ quarantine the candidate, notify. **Previous unhealthy or
   refused** ⇒ the **terminal state** (finding 7): stay stopped, mark the **host**
   failed in `deploy/.auto-update/state` (which suspends auto-update until an operator
   clears it), do not quarantine, and page through an **off-box** path: a healthcheck
   ping whose *silence* alerts, so a dead island still pages.

**State manifest (finding 2):** `aiko_data` holds the db (restored as above) plus the
worker-guard lockfile and markers (both disposable). `mosquitto_data` is **not**
restored, and doesn't need to be: the gateway's paho client uses a clean session, so the
broker holds no backlog for it. Bus traffic while the gateway is down is dropped
exactly as on every manual deploy today (v1's open question 1, answered).

**Bootstrap (finding 6):** compose's `image:` becomes `${ISLAND_IMAGE:?}` for all three
aiko services. That kills `:-edge`, design 16's empty-pin trap. `update.sh` writes
`ISLAND_IMAGE` from `ISLAND_VERSION` for manual deploys. The release that carries this
is the **named one-time compose hand-sync** on each box, done by the operator, which
`preflight-compose-drift.sh` enforces.

## Deliberately out of scope

- **Healthy-but-wrong** releases: the canary soak plus `PROMOTION_HOLD` bound them;
  nothing unwatched can detect them.
- **Push-CD** (CI with SSH to boxes): still rejected. The island pulls.
- **Backward compat floors**: deferred (see §1) together with the CI proof they require.

## Findings ledger (round 1 → v2)

| # | Finding | v2 |
|---|---|---|
| 1 | Health ≠ admission | **Removed**: record-don't-gate; no rewind past `.served` |
| 2 | WAL is three files; no inventory | Folded: `.backup` (no WAL) + manifest |
| 3 | Step order contradicts quarantine | Folded: phases A/B/C; quarantine only on B, or on C with previous healthy |
| 4 | Delay ≠ canary | Folded: `:stable` promoted from the canary's public `/health` |
| 5 | Floor is a graph | **Removed**: no floors in v2 |
| 6 | Compose can't take a digest | Folded: `${ISLAND_IMAGE:?}` + named hand-sync |
| 7 | Double failure undefined | Folded: host-failed terminal state + off-box dead-man page |
| 8 | Health = one of three | Folded |
| 9 | Pre-guard images | Named + enforced by label |

## Open questions for round 2

1. Does the `.served` marker hold under `restart: always`? A crash-looping candidate
   re-runs the entrypoint, but the marker only proves "got past migrate once". Is any
   path to a served write missing from it? (Checked: registrar and chat mount no
   volume, so the gateway is the only durable writer.)
2. Is `started_at` on public `/health` an acceptable disclosure under the island-mark
   activity ruling? It reveals restart times, nothing about users.
3. Should Phase B also require `aiko_connected`? `--network none` makes it false by
   design. Is a bus-less boot enough evidence?
