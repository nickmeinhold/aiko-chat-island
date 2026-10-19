# Design 18: Operator opt-in auto-update (channel, restore-on-failure, compat guard)

**Status:** v4, CANDIDATE for a NEW `/design-temper` series (v3 reached the round cap; see
[`18-TEMPER.md`](18-TEMPER.md)). §1 is SHIPPED (v0.17.0, both islands, 2026-10-19). §2–§3 are
not built. **Bar:** ≤ ~1.5× v1 (1,058 words), every piece removes a failure mode, and **every
Phase B cell names its evidence**.
**Answers:** Nick, 2026-08-16 and 2026-10-10: *"the option to have the server automatically
pull the latest image… it's what I want."* Promoted channel by default, raw `:latest` opt-in.
**enspyr-melb** (`chat.enspyr.co`, where phones ring) is the canary, **enspyr-syd** the test
island. That's deliberate.

## Why v4 is a table, and what the evidence column means

Three rounds found their worst flaw in an unwritten failure arm of Phase B's prose, and
round 3's worst three were created by round 2's folds. A table makes every arm a cell.
And design 18's founding premise ("the old image serves anyway") was false in the design and
in all three strikes, because the code that refuted it (`db._assert_at_head`) was never in
the bundle. So each cell is tagged:

- **[E*n*]**: executed. [`18-evidence/phase-b-evidence.sh`](18-evidence/phase-b-evidence.sh)
  runs the REAL published images (0.16.1 → 0.17.0) on a throwaway volume; output and a
  must-fail arm are in [`phase-b-evidence.out`](18-evidence/phase-b-evidence.out).
- **[C]**: code path cited, not executed. **[A]**: argued only. Strike these hardest.

## 1. Compat guard (SHIPPED, PR#392 → v0.17.0)

One predicate, `migrate.schema_status()`, decides for both boot layers: HEAD admits, UNKNOWN
refuses (`MIGRATE_REFUSE_UNKNOWN_REVISION`), no override, no floors. Admission [E2], refusal
[E4], and admission in production on both islands at deploy. **Consequence the table uses:**
with no floors, an older image NEVER accepts a newer schema [E3], so after a migrating
release has served, "start previous and let the guard decide" has a known answer.

## 2. Channels and promotion

- **`:latest`**: syd and melb follow it. **`:stable`**: every other island's default.
  `AUTO_UPDATE=off` (default; `update_nudge` still nags) | `stable` | `latest`.
- **Per-digest memory** (round 3 #3: one global `last_red_at` froze promotion). An hourly CI
  sampler attributes each sample to the digest the island *reports* (`/health` `build`).
  A red counts only against that digest. An unreachable sample counts red only if the next
  sample is also unreachable, since a planned cutover (≤ budget + rollback) is shorter than
  an hour. "Not on it yet" is no sample.
- **Eligible(R)** = first seen green on both islands ≥ `SOAK_DAYS` (7) ago, no red
  attributed to R, syd's synthetic probe green. Newer releases don't reset it.
- **Promotion is a human act until #4147.** Age is not exercise. The attributable-use signal
  (melb's island-mark activity buckets, keyed to the serving digest) is decided but not
  built. Until it is, CI *proposes* the newest eligible R and `PROMOTE_NOW=<digest>` promotes.
  Followers still update automatically.
- **Holds conduct forward** (round 3 #6). CI commits `holds.json` (orphan branch,
  `GITHUB_TOKEN`, `concurrency:` group) listing digests a red was attributed to after
  promotion, or that melb's watcher rejected. Melb rejected R when melb isn't serving R
  after its window closes. That includes an innocent skip (an A2 retry): a false hold is
fail-closed, and a human clears it. Every watcher reads it in A1 before pulling. syd's window is 2 h
  after melb's. `:stable` never moves backward [E3].

## 3. The watcher: systemd timer, shell, one lock

**The marker.** At the end of lifespan, after `verify_schema()` and before `bus.start()`
(`main.py:229-250`, the first point this process can consume or serve), the gateway APPENDS
`<digest> <container-id> <utc>` to `/data/.served`. **Present(N)** = any line for digest N.
An id mismatch is never absence (round 3 #2). The claim is only "**no gateway data-volume
write was admitted**". Registrar/chat off-box effects before it are accepted loss, as in a
manual deploy (round 3 #5).

**Healthy(X)** = three containers running AND `/health` 200 AND body `aiko_connected: true`
AND `build` = X. The image's `HEALTHCHECK` (`Dockerfile:76`, `curl -f`) can't see the bus
[C], so the watcher reads the body (round 3 #4).

**Lease.** Every run pings the off-box dead-man **iff Healthy(running)**. A planned cutover
renews it first, and its grace exceeds budget + rollback. So a release that dies after
becoming current pages, and so does host-failed.

**One-shot route.** The volume is Docker-managed and the stack is down during cutover, so
backup and restore run as `compose run --rm --no-deps chat-island <cmd>`. `entrypoint.sh` is
`CMD`, not `ENTRYPOINT`, so the override does NOT migrate [E1].

| State | Action | Success → | Failure → | Invariant on entry | Evidence |
|---|---|---|---|---|---|
| **A0** | `flock -n` (shared with `update.sh`) | A1 | exit (busy) | none | [A] `update.sh` takes NO lock today: bootstrap 3 |
| **A1** | resolve channel digest N; read `holds.json` | A2 | exit; lease iff Healthy | stack untouched | [A] |
| **A2** | pull N; record P; drift preflight; disk ≥ 2× db; manifest; `migrates := head(N) ≠ stamp` | B1 | retry next poll, no quarantine | stack untouched | drift: measured both boxes 2026-10-19; stamp/head [E1]/[E2] |
| **B1** | renew lease; `compose down` | B2 | `up -d` P → A0 next poll | lease renewed | [A] `down`, not `stop`: a stopped `restart: always` container returns on daemon restart |
| **B2** | one-shot `.backup` → `pre-N.db`; `integrity_check` | B3 | `up -d` P on the untouched set, retry, no quarantine | stack down; `.served` intact | [E1] (round 3 #1: backup now precedes any deletion) |
| **B3** | truncate `.served` | B4 | → B2's failure arm | **checked `pre-N.db` exists** | [A] |
| **B4** | `image.env` = N; `up -d`; wait ≤ `AUTO_UPDATE_BUDGET` (600 s) | **CURRENT(N)**: lease | B5 | migrating = waiting | Healthy: [E2] admit; body observed live 2026-10-19 |
| **B5** | `compose down`; read Present(N) | absent → B5a · present ∧ migrates → **T** · present ∧ ¬migrates → B6 | **T** | stack down | present ∧ migrates ⇒ P refuses: [E3] |
| **B5a** | one-shot restore of `aiko.db`, `-wal`, `-shm` from `pre-N.db` | B6 | **T** | `pre-N.db` checked (B3) | [E5]; must-fail arm goes red without it |
| **B6** | `image.env` = P; `up -d`; wait budget | **QUARANTINE(N)**: notify | `compose down` → **T** | data at P's schema | admit [E5]; refuse [E3] |
| **T** | stay down; `state` = host-failed; diagnostics; no lease ping (pages) | human | n/a | nothing deleted after B2 | [A] |

**B5's present ∧ migrates → T is new.** It skips a boot whose refusal is certain [E3], so
there is no doomed restart loop during the outage. `migrates` is computed in A2, before
anything stops.

**T's diagnostics:** phase, N, P, stamp before/after, `.served` lines (with time), per-service
exit codes, `/health` bodies. **Runbook:** fix forward with a newer release, or restore
`pre-N.db` and accept losing writes since the first `.served` line for N. Don't quote the
image's refusal text: 0.16.1's says *"run `alembic upgrade head`"*, which can't work
(captured in E3; #393).

**State manifest** (round 3 #6): `deploy/state-manifest.txt`, one line per writable mount:
`service path class reason`, class ∈ {restored, disposable, external, forbidden}. A2 fails
on any mount in `compose config` not listed. Today: `aiko_data` restored (db set; `.served`
and the worker lock disposable); `mosquitto_data` disposable (paho clean session).

**Bootstrap, once per box, in order:**
1. `ISLAND_VERSION` out of SOPS (**ct#5165**, prerequisite: today every release costs a
   Touch ID re-lift PR, e.g. #397).
2. Sync a compose file with `image: ${ISLAND_IMAGE:?}` (kills `:-edge`).
3. `update.sh` takes the same `flock` and writes `image.env`.
4. `AUTO_UPDATE_PAGE_URL`; 5. only then `AUTO_UPDATE`.

## Named tradeoffs

- **Healthy-but-wrong** past one probe and the canary's real use is the canary operator's
  (Nick's). No smoke suite (the design-16 ratchet).
- **Promotion waits on #4147** for an exercise signal. Until then it's a human act.
- **The probe account** needs an island-mark exclusion flag that the #4147 count honours.
  Until #4147 there is no count to pollute.
- CI holds a probe password (a user credential) and writes `holds.json` with `GITHUB_TOKEN`
  only. **Push-CD stays rejected:** the island pulls.

## Round-policy for the v4 series (decided, not deferred)

Panel: Noether, Carnot, Tesla. Cap 3. **Stop** after a round whose every fatal flaw is a
narrow fix inside an existing cell. **Continue** while a flaw names a missing cell or arm.
**Named uncertainty:** does the evidence column change where reviewers aim? **Check:** tag
each finding's cell. Prediction: ≥ 70% land in [A] cells. If they spread evenly, the tags
aren't steering review and v5 should execute more cells instead of labelling them.
