# Design 18: Operator opt-in auto-update (channel, restore-on-failure, compat guard)

**Status:** v6. **The v4 temper series is CLOSED** (r1, r2, r3 RECAST 3/3; see
[`18-TEMPER.md`](18-TEMPER.md)). Per the round policy below, the next move is to BUILD: §2 + the
marker first, then the watcher as code. r3's findings are its acceptance tests. §1 SHIPPED (v0.17.0, both islands, 2026-10-19). §2–§4 not
built. **Bar:** ≤ ~1.5× v1 (1,058 words); every piece removes a failure mode; every [E] tag
cites the harness LINE that proves it; every v3 branch accounted for (§ ledger).
**Answers:** Nick, 2026-08-16 and 2026-10-10: *"the option to have the server automatically
pull the latest image… it's what I want."* **enspyr-melb** (where phones ring) is the canary,
**enspyr-syd** the test island: Nick's ruling, deliberate.

## Evidence

**[E:*line*]** = a line of [`18-evidence/phase-b-evidence.sh`](18-evidence/phase-b-evidence.sh),
run against the REAL published images (0.16.1 → 0.17.0); output and a must-fail arm are in
[`phase-b-evidence.out`](18-evidence/phase-b-evidence.out). Backups live on a HOST bind mount
(line 39), as the design's do. **[C]** = code cited. **[A]** = argued: strike hardest.

## 1. Compat guard (SHIPPED, PR#392)

`migrate.schema_status()` decides for both boot layers: HEAD admits [E:75], UNKNOWN refuses
[E:86], no floors. So the previous image refuses a migrated db, and its real `CMD` exits
without binding [E:76, E:82]. Once a migrating release has served, rollback has a known answer.

## 2. The volume sentinel (image-side, buildable now, needed regardless)

`migrate.run()` writes `/data/.initialized` when it creates a db on an empty volume, and
**refuses to create a db when the sentinel exists and `aiko.db` doesn't** (`MIGRATE_REFUSE_
MISSING_DB`, no override). Today an empty volume is silently reborn as an empty island (the
harness setup does exactly that, [E:69]). The sentinel closes that for every actuator at once:
the watcher, `update.sh`, a 3am hand command. One door, so the table needs no "db exists?"
guards. [A until built; its tests need a refusal AND an admission of a genuinely fresh volume.]

## 3. Identity

The **digest** is the release. Install, quarantine and holds key on it (a rebuild of one commit
resolves apt/pip anew, so it's a different release). The pulled image's own
`/app/build-info.json` gives `sha` and `ref` without starting it [E:91]; `head` comes from its
own scripts [E:94-95]. `/health` `build.git_sha` = the digest's `sha` proves the island runs
what was installed. `ref` is the tag the drift preflight already takes (`update.sh:246`) [C].

## 4. Channels and promotion

- **`:latest`**: syd and melb. **`:stable`**: everyone else. `AUTO_UPDATE=off` (default) |
  `stable` | `latest`; it refuses to turn on without `AUTO_UPDATE_PAGE_URL`.
- **Memory per digest.** An hourly CI sampler attributes each sample to the digest whose `sha`
  the island reports. An island unreachable on two consecutive samples **pauses all promotion**.
- **Eligible(R)**: first seen green on both islands ≥ 7 days ago (`SOAK_DAYS`), no red, syd's
  probe green; newer releases don't reset it. **Promotion is a human act** (`PROMOTE_NOW`)
  until #4147 gives an attributable-use signal.
- **Holds conduct forward:** `holds.json` (orphan branch, `GITHUB_TOKEN`, `concurrency:`), read
  by every A1. **syd follows melb:** syd's A1 requires melb Healthy on N. `:stable` never moves
  backward. `ghcr-retention` keeps every digest that is any island's `current` or `previous`.

## 5. The watcher

**One actuator:** `COMPOSE="docker compose -f docker-compose.yml --env-file .env --env-file
image.env"`, shared by the watcher and `update.sh`; `image.env` holds `ISLAND_IMAGE=<repo>@<digest>`,
interpolated by all three aiko services. **Journal** `deploy/.auto-update/journal.json`
(tmp + fsync + rename): `current`, `previous`, `txn` {id, N, P, migrates, phase}, `quarantine`,
`host_failed` {reason, N} | null. **Marker:** immediately after `verify_schema()`
(`main.py:229`) [C], the gateway appends `<sha> <container> <utc>` to `/data/.served` and
fsyncs; a failed write raises, so the container never serves [A]. **Healthy(X)** = three
containers up ∧ `/health` 200 ∧ `aiko_connected` ∧ `git_sha` = X's (`HEALTHCHECK` can't see
the bus, `Dockerfile:76`) [C]. **Lease:** ping iff Healthy(current); grace = 2 × budget +
restore + 10 min, so a stuck update pages.

| State | Action | Success → | Failure → | Evidence |
|---|---|---|---|---|
| **A0** | `flock -n`; read journal | `host_failed` ⇒ exit, no ping · `txn` ⇒ **R** · else A1 | busy ⇒ exit | [A] |
| **A1** | resolve channel → N | N = current, quarantined, held, or (syd) melb not Healthy on N ⇒ **steady**: lease iff Healthy · else A2 | exit | [E:91] |
| **A2** | pull N; read N's sha/ref/head; P := current, re-pull if pruned; `stamp = head(P)`; preflight(N.ref); disk; manifest | `txn` := {N, P, migrates := head(N) ≠ stamp, B1} → B1 | retry, never quarantine · P unfetchable or `stamp ≠ head(P)` ⇒ refuse update + notify | [E:94-95, E:91] |
| **B1** | renew lease; `compose down` (`stop` revives on daemon restart) | B2 | `up -d` P; clear `txn` | [A] |
| **B2** | one-shot `.backup` → host `pre-<txn>.db`; `integrity_check`; keep last 2 | B3 | `up -d` P; clear `txn`; never quarantine | [E:71-72] |
| **B3** | truncate `.served` | B4 | as B2 | [A] |
| **B4** | `ISLAND_IMAGE` := N; `up -d`; wait ≤ 600 s | **CURRENT(N)**: previous := P, clear `txn` | B5 | [E:73-75] |
| **B5** | `compose down`; read marker | absent ⇒ B5a · present ∧ ¬migrates ⇒ B6 · present ∧ migrates ⇒ B4′ | unreadable ⇒ T | [E:76, E:82] |
| **B4′** | N again, second budget (slow broker ≠ bad release) | **CURRENT(N)** | T | [A] |
| **B5a** | one-shot: replace `aiko.db` from `pre-<txn>.db` (WAL off, `db.py:48`) | B6 | T | [E:87-88] |
| **B6** | `ISLAND_IMAGE` := P; `up -d`; wait | **QUARANTINE(N)**, clear `txn`, notify | T | [E:88, E:82] |
| **R** | after watcher death: **observe only**. Healthy(N) ⇒ CURRENT(N) · Healthy(P) ∧ phase ≥ B5 ⇒ QUARANTINE(N) · phase ≤ B3 (db untouched) ⇒ `up -d` P, clear `txn`, as B2's failure arm · else T(`interrupted@phase`) | as named | T | [A] |
| **T** | `host_failed` := {reason, N}; `compose down`; no ping (pages) | human | n/a | [A] |

**R never moves data.** It may start P only where B2's failure arm would (the db is untouched
before B3). Data moves only inside one uninterrupted run. An interrupted
rollback is a human's call, because only a human can see what P served while booting.

**Leaving T: one command.** `auto-update clear` quarantines N, clears `txn` and `host_failed`.
`--retry` keeps N eligible. **Runbook:** read the diagnostics (`txn`, shas, stamps, `.served`
lines, exits, `/health` bodies). Fix forward, or restore host `pre-<txn>.db` (losing writes
since N's first `.served` line). The sentinel stops a hand `up` from serving an empty island
mid-repair. Don't quote the old image's advice: 0.16.1 says *"run `alembic upgrade head`"*,
which can't work (#393).

**Manifest** `deploy/state-manifest.txt` (`service path class reason`): A2 fails on any unlisted
mount. `aiko_data` restored; `mosquitto_data` disposable (clean session).

**Bootstrap:** (1) `ISLAND_VERSION` out of SOPS (**ct#5165**: today every release costs a Touch ID
re-lift PR, #397); (2) compose `image: ${ISLAND_IMAGE:?}` on all three services; (3) `update.sh`
uses the shared `COMPOSE`, takes the lock, writes `image.env` + `current`; (4) page URL;
(5) `AUTO_UPDATE`. A hand `docker compose` bypasses the lock (the operator's tradeoff), but not
the sentinel.

## Ledger: v3 → v6

compare with running → A1 · re-pull P → A2 · Phase A never quarantines → A2 · backup failure → B2
· absent ⇒ restore → B5a · present ⇒ previous → B5/B6 · quarantine superseded only by newer → A1
(quarantine is per digest) · host-failed suspends until cleared → A0 + T + `clear` · keep last 2 →
B2 · page URL required → §4 · lease → §5. **Dropped deliberately:** v5's five "db exists?" guards
(→ §2 sentinel) and R's data dispatch (→ observe only).

## Named tradeoffs

Healthy-but-wrong past one probe and the canary is the canary operator's (Nick's). There's no
smoke suite. Promotion waits on #4147. CI writes only with `GITHUB_TOKEN`. Push-CD stays rejected.

## Round 3 policy (last)

Panel: Noether, Carnot, Tesla. If r3 names a missing cell, **stop designing and build**: (a) the
sentinel and the marker (§2, §5, useful without a watcher), then (b) the journal and the watcher,
struck as code. The bundle comes from `origin/main`, with the harness re-run first.
