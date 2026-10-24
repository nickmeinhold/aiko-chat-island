# Design 20: what a volume can witness about itself

Answers [#400](https://github.com/nickmeinhold/aiko-chat-island/issues/400) Q1–Q3 and
Q7, with Q4–Q5 noted where the measurements reached them. Status: **v3, PROPOSED. Temper CAPPED after 3 rounds (RECAST 4/4 each); the spine below is affirmed unanimously, and seven listed mechanism folds from round 3 are NOT yet applied here: see [`20-TEMPER.md` § Round 3](20-TEMPER.md). Read them before building. The most important: the migrator runs from `CMD`, so the lineage check must also run in the app lifespan.**
v1 and v2 were each struck by four families, and both came back **RECAST 4/4**
([`20-TEMPER.md`](20-TEMPER.md)). This version folds round 2. Each fold is marked
*(T2-n)*, where n is the flaw's number in that round's synthesis.

## The question, and the answer

The volume sentinel (#399, v0.17.1) stops a lived volume from silently becoming a new,
empty island. A record on a volume can say one thing: *an island has lived here*. It
can't say *this is the volume that should be here*: from inside the container, a fresh
volume and a first standup look the same.

**A record inside the volume proves the volume has a past. A record outside it, the
tally, proves the volume belongs to this host's island. It proves the island's
_lineage_, never how recent the copy is.** A stale copy of this island is
indistinguishable from a legitimate restore, and is served by design.

## The rule: check each fact where it is fixed

| Fact | Can change | So it is guarded | by |
|---|---|---|---|
| **the image contains the guard** | only at container creation (a container keeps its image ID; a moved tag moves a name) | at **creation** | **the image floor**, in the wrapper |
| **the lineage on the volume** (sentinel, db) | at any time: a stopped container's volume can be rewritten in place, and `restart: always` brings it back with no wrapper (Tesla, *T2-1*) | at **every start** | **the migrator**, in the image |

v2 assigned "which volume is mounted" to the wrapper, at creation. Round 2 showed the
volume *object* is fixed at creation but its *contents* are not, and the tally vouches
for contents. So in v3 **the tally is checked by the migrator on every start, and the
wrapper keeps exactly one job: the floor.** Two facts, two places, one predicate each.

## What was measured

All on 2026-10-24. Read-only on both live islands; throwaway probes on syd.

| | syd (`chat.imagineering.cc`) | melb (`chat.enspyr.co`) |
|---|---|---|
| `.aiko.db.initialized` content | `2026-10-20T06:01:11.810364+00:00` | `2026-10-20T06:02:12.263441+00:00` |
| `aiko_data` `CreatedAt` | `2026-07-05T00:17:38Z` | `2026-07-11T01:28:53Z` |
| `.aiko-worker.lock` | 0 B, 2026-08-02 | 0 B, 2026-08-02 |
| `-wal` / `-shm` / `-journal` | none | none |

1. **A missing `external: true` volume makes Compose refuse.** It does not create one.
   A positive control (volume present) started.
2. **`docker volume rm` refuses while any container references the volume, including a
   stopped one; `-f` doesn't override it.** The control with the container removed
   succeeded.
3. **Images carry no OCI labels.** Every release image carries `/app/build-info.json`
   (`git_sha`, `ref`, `built_at`), readable with `--entrypoint cat`.
4. **An execution probe tells guarded images from unguarded ones by behaviour** (*T2-4*).
   - Setup: a throwaway volume holding only a sentinel; the image runs with its **real
     entrypoint**, `--network none`, `ENVIRONMENT=development` (which disarms the
     prod-secret config checks, not the sentinel logic).
   - **0.16.1 and 0.17.0 created a fresh `aiko.db` and kept running.** That is Q7's
     failure, reproduced on demand.
   - **0.17.1 and 0.17.2 exited 1 with `MIGRATE_REFUSE_MISSING_DB` and created
     nothing.**
   - Control: 0.17.2 on an empty volume warned "creating a NEW, empty island" and
     provisioned.
   - A first attempt without `ENVIRONMENT=development` failed on *every* image with a
     config error. That's a useful negative control: the probe must reach the migrator
     before its result means anything.
5. Only `chat-island` mounts `aiko_data`. `update.sh`'s backup copies `aiko.db` and **not**
   the sentinel (*T2-2*).

## Q7: the image floor *(Nick 2026-10-24)*

**The wrapper refuses to create the gateway container from an image that doesn't pass
the execution probe.** No production image runs below the floor, so every start runs
the migrator's single predicate.

- **The probe is measurement 4**, run against the exact image about to be used: a
  throwaway volume with only a sentinel, `--network none`, the real entrypoint. **Pass**
  means a non-zero exit, `MIGRATE_REFUSE_MISSING_DB` in its output, and no `aiko.db`
  created. It tests the guard *running through the entrypoint*, not the name of the
  guard (Noether, Tesla, *T2-4*). Results are cached by image ID in the deploy dir, so a
  repeat `up` doesn't pay for the probe again. Once the tally lands, the probe also
  needs a mismatching tally to refuse (see below), so the floor rises with that release.
- **The image probed is the image used** (Carnot, *T2-5*). Under the wrapper's lock:
  1. pull;
  2. resolve the image ID;
  3. probe that ID;
  4. `up --pull never --no-build`;
  5. afterwards, assert the gateway container's image ID equals the probed ID.

  If the post-condition fails, stop the container and refuse. A digest pin makes
  steps 1–2 immutable.
- **A reused container is checked too** (Noether, *T2-6*). `up` can start an existing
  container without recreating it, so the floor applies to *the existing container's*
  image ID. `--no-recreate` is refused for the gateway.
- **No override flag.** The wrapper may refuse, never permit.
- **Below the floor** is an explicit offline procedure (a fresh island, or forensic work
  on a copy, by hand), documented in the rollback runbook.
- **Named bypass:** a hand `docker run`, or a bare compose with the pin exported (#5186),
  never meets the wrapper. Same class as #401's limit.

## The tally, v3: checked inside the image

**What it holds:** `witness/volume.tally` in the deploy dir, with three fields:
`<lineage = sentinel content> <db path> <island domain>`. The db path lets the migrator
compare against the file it is actually opening (Carnot, *T2-1*). The domain is shown to
the operator; it is never used as a secret. `CreatedAt` is recorded for forensics only
and never compared.

**How the migrator sees it:** compose mounts `./witness` **as a directory**, read-only,
at `/witness`. A single-file bind would keep the old inode across the tmp+rename write,
so a directory is required (Tesla). The gateway gets `WITNESS_DIR=/witness`. When
`WITNESS_DIR` is set, `migrate.run()` does this before any connect:

- tally missing or unreadable → **refuse** (`MIGRATE_REFUSE_UNENROLLED`);
- the db path it is opening ≠ the tally's path → **refuse**;
- sentinel present and ≠ the tally's lineage → **refuse** (`MIGRATE_REFUSE_FOREIGN_LINEAGE`);
- sentinel absent → **refuse**, with "run `compose.sh enroll`";
- otherwise, today's checks unchanged (lived evidence, the `users` check, `mode=rw`).

With `WITNESS_DIR` unset (dev, tests, the probe), behaviour is today's. That is
fail-closed where it's configured, and the drift guard refuses a box whose compose has
dropped the mount.

**What this buys:**
- **`restart: always` is covered.** A daemon restart after an in-place rewrite of the
  volume, or after its witnesses were wiped, now refuses on the next start (*T2-1*).
- **The `run -e` / `-v` blindness disappears for the tally.** The migrator checks the
  db it actually opened, whatever the command line said, so `docker compose config` is
  no longer part of the design (*T2-3*).
- **What's left for the wrapper:** an `--entrypoint` override skips the migrator
  entirely, so `compose.sh run` refuses `--entrypoint` for the gateway, and refuses
  `-e WITNESS_DIR`/`ENVIRONMENT` overrides. Everything else `run` does is seen by the
  migrator.

### Enrolment: one ceremony, no `pending` *(T2-2, Carnot's simplification)*

`pending` is deleted. Every way a volume becomes enrolled goes through
`compose.sh enroll`, with the stack down:

| Volume holds | `enroll` does | Confirmation |
|---|---|---|
| sentinel (a live island, a restore that kept the dotfiles) | writes the tally from it | island mark + `--yes-this-is-<domain>` |
| db with `users`, no sentinel (a db-only backup restored into a new volume; a lock-only volume) | runs **migrate only** (`python -m aiko_gateway.migrate`, no server) from the floor image with `WITNESS_DIR` unset; that path writes the sentinel through today's logic; then writes the tally from it | island mark + `--yes-this-is-<domain>` |
| nothing | migrate-only provisions a fresh db and sentinel; writes the tally | `--new-island <domain>` |
| sentinel ≠ an existing tally | refuses; only `--replace-lineage <domain>` proceeds, and it prints both lineages | explicit, separate flag |

- **The island mark** (Tesla, *T2-7*). Two timestamps 61 seconds apart can't be told
  apart at 3am, so `enroll` shows recognisable facts read from the db through the
  read-only helper: the user count, the newest message time, the island's own
  domain/manifest name. The flag names the domain, not the timestamp.
- **Crash safety without `pending`.** A crash before the tally is written leaves no
  tally, so the next start refuses and `enroll` runs again. It's idempotent: the
  sentinel now exists, so it takes the first row. The tally is written atomically (tmp,
  fsync, rename, fsync the directory).
- **First standup** is `standup.sh` → `enroll --new-island` → `up`. `standup` never
  offers itself on a volume that has witnesses (Tesla).
- **The lock-only tradeoff from v2 dissolves.** Row 2 enrols such a volume via
  migrate-only, which today's code already adopts.

### Restores keep their lineage *(T2-2)*

`update.sh`'s backup copies `.aiko.db.initialized` beside `aiko.db`
(`backups/aiko.db.preupdate-<ts>` + `.initialized`), and the restore runbook restores
both, with the stack **down** (`compose.sh down` removes the containers, which also
prevents a mid-restore daemon restart). A restore into the same volume keeps the
lineage, and the tally matches. A restore into a new volume from a db-only backup goes
through `enroll` row 2. Runbook line: *after restoring, remove any `aiko.db-journal`
beside the restored file* (Q5).

### Durability *(T2-8)*

`witness/` joins `image.env` as a **box-local, never-synced** path. The drift preflight
lists it as box state, not tag state. Any refresh of the deploy dir (design 18's watcher
included) must leave it alone, under the same rule and test that protect `image.env`.
Losing it makes every start refuse, both islands if both lose it. That is loud, and it's
recovered by `enroll`.

## Q3: states × transitions *(T2-6)*

**Transitions:**
- **create**: a new container, through the wrapper;
- **reuse**: `up` or `start` of an existing container, through the wrapper;
- **restart**: `restart: always`, a daemon restart, a bare `docker start`; no wrapper;
- **bypass**: a hand `docker run`, or a bare compose with the pin exported.

| Volume state | create | reuse | restart | bypass |
|---|---|---|---|---|
| **S1** right volume, intact | serves | serves | serves | out of model |
| **S2** recreated empty | refused: migrator (unenrolled lineage) | n/a: a recreate needs every container removed (meas. 2) | n/a, same | out of model |
| **S3** foreign lineage | refused: migrator | refused: migrator | **refused: migrator** | out of model |
| **S4** stale copy of this lineage | serves, by design | serves | serves | out of model |
| **S5a** db missing / 0-byte / no `users` (witnesses intact) | refused: migrator | refused | refused | out of model |
| **S5b** whole volume wiped in place (witnesses gone too) | refused: migrator (sentinel absent, tally present) | refused | **refused** | out of model |
| **S5c** truncated, but `users` still readable | **not detected** until Q4's `quick_check` ships | same | same | out of model |
| **S6** another db path or mount | refused: migrator (path ≠ tally) | refused | refused | out of model |
| **S7** tally lost | refused: migrator (unenrolled) | refused | refused | out of model |

On the watcher, a refusal that loops under `restart: always` is a **host-class failure**.
Design 18's "host failed, not release failed" state applies, so S5 or S7 must never
quarantine a good digest (Maxwell, *T2-6*).

## Q1, Q2, Q4, Q6

- **Q2:** keep the per-directory lock as a production feature (it catches a *fresh*
  file beside an island). Since the tally now records the db path, S6 is also caught
  for a *copied* island, which the lock never caught (Tesla's correction).
- **Q1:** keep the lock as a witness, renamed **the directory witness**. Revisit once
  the tally ships: its path field covers S6 from outside the volume.
- **Q4:** `quick_check` gets its own PR, timed on both volumes. S5c above is
  honestly marked as not covered until then.
- **Q6:** belongs to #393.

## What gets built

The work is in order, because the floor depends on the tally release:
1. **Image PR** (cage-match: trust boundary):
   - `WITNESS_DIR` tally checks in `migrate.run()`, with the new refusal codes;
   - a migrate-only mode;
   - tests, including a fixture for every state in the Q3 matrix.
2. **`deploy/` PR**:
   - the floor probe with the identity post-condition and cache;
   - the `run` override refusals;
   - `enroll` and the island mark;
   - the `witness/` mount in compose;
   - backups carrying the sentinel;
   - runbook changes.
3. **Rollout:** release the image → hand-sync each box (one sync, together with #5186
   and design 18 (b)) → `enroll` syd and melb (row 1, against the values measured above)
   → raise the floor so it requires the tally refusal.

## Questions for round 3

1. Does moving the tally check into the image re-create v1's problem (a second copy of
   the rule)? Or is it now the *only* copy, with the wrapper holding only the floor?
2. Is the `--entrypoint` / `WITNESS_DIR` / `ENVIRONMENT` refusal list for `compose.sh
   run` complete? What else can a `run` flag do to skip the migrator?
3. Does a migrate-only `enroll` on a volume with no sentinel run any code path that
   could adopt the wrong data? It reuses today's adopt-or-provision logic, without the
   tally.
