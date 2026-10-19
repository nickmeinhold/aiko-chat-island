# Design 18: Operator opt-in auto-update (channel, restore-on-failure, compat guard)

**Status:** v5, for round 2 of the v4 series (r1 RECAST 3/3; see [`18-TEMPER.md`](18-TEMPER.md)).
§1 is SHIPPED (v0.17.0, both islands, 2026-10-19). §2–§3 are not built. **Bar:** ≤ ~1.5× v1
(1,058 words), every piece removes a failure mode, **every cell names its evidence**, and
**every v3 branch is accounted for** (§ ledger).
**Answers:** Nick, 2026-08-16 and 2026-10-10: *"the option to have the server automatically
pull the latest image… it's what I want."* Promoted channel by default, raw `:latest` opt-in.
**enspyr-melb** (where phones ring) is the canary, **enspyr-syd** the test island. Deliberate.

## Evidence tags

**[E*n*]** executed: [`18-evidence/phase-b-evidence.sh`](18-evidence/phase-b-evidence.sh) runs the
REAL published images (0.16.1 → 0.17.0) on a throwaway volume; output and a must-fail arm are in
[`phase-b-evidence.out`](18-evidence/phase-b-evidence.out). **[C]** code cited. **[A]** argued:
strike these hardest. The bundle is built from `origin/main` (r1's was stale; see 18-TEMPER).

## 1. Compat guard (SHIPPED, PR#392 → v0.17.0)

`migrate.schema_status()` decides for both boot layers: HEAD admits [E2], UNKNOWN refuses
[E4], no override, no floors. So an older image never accepts a newer schema, and its real
`CMD` exits without binding [E3, E3′]. **The table uses this:** once a migrating release has
served, starting the previous image has a known answer.

## 2. Identity: pull by digest, compare by commit

The registry names an image by **digest**; the running app reports **`build.git_sha`** (baked in
`/app/build-info.json`, served at `/health`). The watcher pulls and pins by digest, and reads
the pulled image's `build-info.json` without starting it, giving `sha` and `ref` [E6]. Every
comparison (Healthy, marker, CI memory, quarantine, holds) uses `sha`. `ref` is the tag the
drift preflight already takes (`update.sh:246`). A rebuild of one commit is one release; the
base image is digest-pinned (`Dockerfile:14-22`) [C].

## 3. Channels and promotion

- **`:latest`**: syd and melb. **`:stable`**: everyone else. `AUTO_UPDATE=off` (default;
  `update_nudge` still nags) | `stable` | `latest`. It refuses to turn on without
  `AUTO_UPDATE_PAGE_URL`.
- **Per-release memory, keyed by `sha`.** An hourly CI sampler attributes each sample to the
  `sha` the island reports. An island unreachable on two consecutive samples **pauses all
  promotion** until it answers. It's island-scoped and fail-closed, and needs no guess at
  which release to blame. "Not on it yet" is no sample.
- **Eligible(R)** = first seen green on both ≥ `SOAK_DAYS` (7) ago, no red on R, syd's
  probe green. Newer releases don't reset it. **Promotion is a human act** (`PROMOTE_NOW`)
  until #4147 provides an attributable-use signal. Followers still update automatically.
- **Holds conduct forward.** CI commits `holds.json` (orphan branch, `GITHUB_TOKEN`,
  `concurrency:`) with post-promotion reds and canary rejections. Every A1 reads it. A false
  hold is fail-closed. **syd follows melb, not the tag:** syd's A1 requires melb to report
  Healthy on N's `sha`. `:stable` never moves backward [E3].

## 4. The watcher: timer, shell, lock, journal

**Journal** `deploy/.auto-update/journal.json`, written tmp + fsync + rename: `current`
{digest, sha, head}; `txn` {id, N, P, stamp, backup, phase} or null; `quarantine` [sha];
`host_failed` bool. `phase` is rewritten on entry to every B cell. **Every run reads it first.**
It is the only memory, so the watcher can die anywhere.

**The empty-island invariant.** No `up -d` of any image unless `aiko.db` exists and passes
`integrity_check`. The entrypoint migrates an EMPTY volume into a fresh database and serves it
(the harness's setup step does exactly this [E-setup]), so a missing db is an island silently
reborn empty. Every `up -d` cell (B1-fail, B2-fail, B4, B4′, B6) checks this first; failure ⇒ T [A].

**Marker.** At the end of lifespan, after `verify_schema()` and before `bus.start()`
(`main.py:229-250`) [C], the gateway appends `<sha> <container-id> <utc>` to `/data/.served`
and fsyncs. **A failed write raises, so the container never serves** [A, needs code]. Present(N)
= any line for N's `sha`. A container-id mismatch is never absence. An unreadable marker ⇒ T.
The claim is only "no gateway data-volume write was admitted"; registrar/chat off-box effects
are accepted loss, as in a manual deploy.

**Healthy(X)** = three containers running ∧ `/health` 200 ∧ `aiko_connected` ∧ `build.git_sha`
= X.sha. The image `HEALTHCHECK` can't see the bus (`Dockerfile:76`) [C]. **Lease:** ping iff
Healthy(current), on every run.

**One-shot route:** backup/restore/identity run as `compose run --rm --no-deps chat-island
<cmd>`. `CMD` isn't `ENTRYPOINT`, so nothing migrates [E1].

| State | Action | Success → | Failure → | Invariant on entry | Evidence |
|---|---|---|---|---|---|
| **A0** | `flock -n` (with `update.sh`); read journal | `host_failed` ⇒ exit, **no ping** (pages until a human clears) · `txn` ≠ null ⇒ **R** · else A1 | busy ⇒ exit | none | [A]; `update.sh` has no lock today (bootstrap 3) |
| **A1** | resolve channel digest → N | N.sha = current.sha, or N in `quarantine`/holds, or (syd) melb not Healthy on N ⇒ **steady**: lease iff Healthy, exit · else A2 | exit; lease iff Healthy | stack untouched | identity [E6] |
| **A2** | pull N; read N's sha/ref/head; verify running = current, re-pull P by digest if pruned; `stamp = head(P)`; preflight(N.ref); disk ≥ 2× db; manifest; `migrates := head(N) ≠ stamp` | write `txn` (phase B1) → B1 | retry next poll, **never quarantine** · `stamp ≠ head(P)` ⇒ T (no rollback target) | stack untouched | [E6], [E1]; preflight measured live 2026-10-19 |
| **B1** | renew lease; `compose down` (not `stop`: `restart: always` returns on daemon restart) | B2 | `up -d` P; clear `txn` | `txn` durable | [A] |
| **B2** | one-shot `.backup` → `pre-<txn>.db`; `integrity_check`; keep the last 2 txns | B3 | `up -d` P on the untouched db; clear `txn`; **never quarantine** | `.served` intact | [E1] |
| **B3** | truncate `.served` | B4 | as B2's failure | **checked `pre-<txn>.db` exists** | [A] |
| **B4** | `image.env`: `ISLAND_IMAGE=<repo>@<digest>`; `up -d`; wait ≤ 600 s | **CURRENT(N)**: `current := N`, clear `txn`, lease | B5 | migrating = waiting | admit [E2]; identity [E6] |
| **B5** | `compose down`; read marker | absent ⇒ B5a · present ∧ ¬migrates ⇒ B6 · present ∧ migrates ⇒ **B4′** | unreadable ⇒ T | stack down | P's refusal certain [E3, E3′] |
| **B4′** | N again: `up -d` N, second budget (a slow broker isn't a bad release) | **CURRENT(N)** | T | data at head(N) | [A] |
| **B5a** | one-shot: delete `aiko.db` and any stray sidecars (WAL is off, `db.py:48`), copy `pre-<txn>.db` | B6 | T | backup checked | [E5]; must-fail arm |
| **B6** | `image.env` = P; `up -d`; wait budget | **QUARANTINE**: `quarantine += N.sha` (only a newer release supersedes), clear `txn`, notify | `compose down` → T | data at head(P) | admit [E5]; refuse [E3′] |
| **R** | recovery after watcher death: `compose down`; resume by `txn.phase`: B1–B3 ⇒ B2's failure arm · B4, B4′ ⇒ B5 · B5a, B6 ⇒ redo B5a (idempotent: the backup is the source), then B6 | per resumed cell | T | `txn` durable; empty-island invariant | [A] |
| **T** | `host_failed := true` (fsync); stay down; no quarantine | human | n/a | nothing deleted after B2 | [A] |

**T runbook:** diagnostics (`txn`, both shas, stamp before/after, `.served` lines, per-service
exits, `/health` bodies). Fix forward with a newer release, or restore `pre-<txn>.db` and
accept losing writes since the first `.served` line for N. Then clear `host_failed`. Don't quote
the image's refusal text: 0.16.1 says *"run `alembic upgrade head`"*, which can't work (E3; #393).

**State manifest:** `deploy/state-manifest.txt`, `service path class reason`, class ∈ {restored,
disposable, external, forbidden}. A2 fails on any unlisted mount. `aiko_data` restored
(`.served` and the worker lock disposable); `mosquitto_data` disposable (clean session).

**Bootstrap:** (1) `ISLAND_VERSION` out of SOPS, **ct#5165** (today each release costs a Touch ID
re-lift PR, #397); (2) compose `image: ${ISLAND_IMAGE:?}` with a second `--env-file image.env`;
(3) `update.sh` takes the lock and writes `image.env` + journal `current`; (4) page URL;
(5) `AUTO_UPDATE`. A hand `docker compose` bypasses the lock: the operator's tradeoff.

## Ledger: v3's branches, accounted for

v4 lost four branches, and two of them were r1's headline flaws. Each v3 branch → its v5 cell:
compare with running → A1 steady · re-pull P if pruned → A2 · Phase A never quarantines → A2 ·
backup failure → B2 · absent ⇒ restore → B5a · present ⇒ previous → B5/B6 · quarantine,
superseded only by newer → B6 · host-failed suspends until cleared → A0 + T · keep last 2 → B2
· page URL required → §3 · lease every run → §4.

## Named tradeoffs

Healthy-but-wrong past one probe and the canary is the canary operator's (Nick's). There's no
smoke suite. Promotion waits on #4147. The probe account needs an exclusion flag that #4147's
count honours. CI holds a probe password and writes only with `GITHUB_TOKEN`. Push-CD stays
rejected.

## Round policy (v4 series)

Panel: Noether, Carnot, Tesla. Cap 3. **Stop** when every fatal flaw is a narrow fix inside an
existing cell; **continue** while one names a missing cell. Before each strike: run the harness
from `origin/main`, and re-run the ledger diff. r1's check (≥ 70% of flaws in [A] cells) held
at 12/17. r2 tests whether R, B4′ and T, the new [A] cells, draw the fire.
