# Design 20: what a volume can witness about itself

Answers [#400](https://github.com/nickmeinhold/aiko-chat-island/issues/400) Q1–Q3 and
Q7, with Q4–Q5 noted where the measurements reached them.

**Status: v5 = v4 + the ceremony-record addendum** ([§ `--enroll` needs evidence, not
absence](#--enroll-needs-evidence-not-absence-v5), from PR#404's capped cage-match,
[ct#5189](https://github.com/enspyrco/claude-tasks/issues/5189)). **The addendum is
UN-TEMPERED** until its `/design-temper` round lands in [`20-TEMPER.md`](20-TEMPER.md).

**v4 history.** Four families struck
it three times, and each round came back RECAST 4/4 ([`20-TEMPER.md`](20-TEMPER.md)).
Round 3 affirmed the spine unanimously: one fact per place, the tally checked inside
the image as the only copy of the lineage rule, and the floor as an execution probe.
The round-3 folds marked *(T3-n)* below have **not** been struck by anyone. Nick ruled
on 2026-10-24 that the next independent strike is the image PR's cage-match (required
anyway for a trust boundary), not a fourth design round. Folds from earlier rounds are
marked *(T2-n)*.

## The question, and the answer

The volume sentinel (#399, v0.17.1) stops a lived volume from silently becoming a new,
empty island. A record on a volume can say one thing: *an island has lived here*. It
can't say *this is the volume that should be here*: from inside the container, a fresh
volume and a first standup look the same.

**A record inside the volume proves the volume has a past. A record outside it, the
tally, proves the volume belongs to this host's island. It proves the island's
_lineage_, never how recent the copy is.** A stale copy of this island is
indistinguishable from a legitimate restore, and is served by design.

**Named physical limit** *(T3-7, Carnot)*. The sentinel is metadata sitting *next to*
the db, not an identity stored *inside* it. A foreign `aiko.db` copied under this
volume's own sentinel can't be detected here. The only defence is restore discipline:
the db and its sentinel always travel as a pair.

## The rule: check each fact where it is fixed

| Fact | Can change | So it is guarded | by |
|---|---|---|---|
| **the image contains the guard** | only at container creation (a container keeps its image ID; a moved tag moves only a name) | at **creation**, before the first start | **the image floor**, in the wrapper |
| **the container is armed** (`WITNESS_DIR` set, read-only `/witness` mount) | only at creation | at **creation** (wrapper) and at every start (image refuses when it's missing in production) | both, and the image is the authority |
| **the lineage on the volume** (sentinel, db, path) | at any time: a stopped container's volume can be rewritten in place, and `restart: always` brings it back with no wrapper | at **every start**, by **whatever serves** | **one lineage predicate**, called from `migrate.run()` *and* the app's startup (lifespan) |

**Why the app's startup too** *(T3-1, measured against `Dockerfile:81`)*. The migrator
runs from `CMD ["./entrypoint.sh"]`; the image has no `ENTRYPOINT`. So `compose.sh run
chat-island uvicorn …`, or a `command:` line, replaces the CMD and the migrator never
runs. Making `entrypoint.sh` the ENTRYPOINT is the wrong fix: the same image runs
`registrar` and `chat` by `command:` (`docker-compose.yml:346,364`), and `entrypoint.sh`
never runs `"$@"`. What has to hold is that **nothing *serves* without the check**, and
serving means the gateway app. So the predicate is one function, called from both
`migrate.run()` and lifespan's `verify_schema()`, before any connect, with the same
non-creating discipline. That is the pattern `schema_status()` already follows for the
compat guard (PR#392 r3). A command that isn't the app (`python -c`, `sh`) can skip it,
but it serves nothing.

## What was measured

All on 2026-10-24. Read-only on both live islands; throwaway probes on syd.

| | syd (`chat.imagineering.cc`) | melb (`chat.enspyr.co`) |
|---|---|---|
| `.aiko.db.initialized` content | `2026-10-20T06:01:11.810364+00:00` | `2026-10-20T06:02:12.263441+00:00` |
| `aiko_data` `CreatedAt` | `2026-07-05T00:17:38Z` | `2026-07-11T01:28:53Z` |
| `.aiko-worker.lock` | 0 B, 2026-08-02 | 0 B, 2026-08-02 |
| `-wal` / `-shm` / `-journal` | none | none |

1. **A missing `external: true` volume makes Compose refuse**, and it creates nothing.
   A positive control (the volume present) started.
2. **`docker volume rm` refuses while any container references the volume, including a
   stopped one; `-f` doesn't override it.** The control with the container removed
   succeeded.
3. **Images carry no OCI labels.** Every release image carries `/app/build-info.json`
   (`git_sha`, `ref`, `built_at`).
4. **An execution probe tells guarded images from unguarded ones by behaviour.**
   - Setup: a throwaway volume holding only a sentinel; the image runs with its default
     command, `--network none`, `ENVIRONMENT=development`.
   - **0.16.1 and 0.17.0 created a fresh `aiko.db` and kept running.**
   - **0.17.1 and 0.17.2 exited 1 with `MIGRATE_REFUSE_MISSING_DB` and created
     nothing.**
   - Control: 0.17.2 on an empty volume provisioned, with the "NEW, empty island"
     warning.
   - Negative control: without `ENVIRONMENT=development`, *every* image failed on the
     production config check before reaching the migrator.
5. Only `chat-island` mounts `aiko_data`. `update.sh`'s backup copies `aiko.db`, **not**
   the sentinel.
6. **The db stores no island identity** *(T3-5)*. No column holds the island's domain
   or id. `island_id` and `passkey_rp_id` are settings, not data.

## Q7: the image floor *(Nick 2026-10-24)*

**The wrapper never starts a gateway container whose image fails the execution probe,
or whose container isn't armed.**

- **Create, inspect, start** *(T3-4, Carnot)*. Under the wrapper's lock:
  1. pull;
  2. resolve the image ID;
  3. probe that ID (or hit the cache);
  4. `create --pull never --no-build` without starting;
  5. inspect the created container: its image ID must equal the probed ID, it must have
     `WITNESS_DIR` set, and it must have a read-only `/witness` mount from the deploy
     dir's `witness/`;
  6. only then `start`.

  A reused container goes through steps 5–6 too (Noether). `--no-recreate` is refused.
  Nothing is checked after the fact: a wrong image never gets a first start.
- **The probe certifies the production boot** *(T3-4, Tesla)*. It runs with
  `ENVIRONMENT=production`, dummy secrets long enough to pass the production config
  checks, `--network none`, and a throwaway volume. Once both islands are enrolled on
  the tally release, the floor policy's next revision gives the probe three cases:
  - (a) a sentinel with no db → must refuse with `MIGRATE_REFUSE_MISSING_DB`, no db
    created;
  - (b) a db whose lineage doesn't match the tally → must refuse with
    `MIGRATE_REFUSE_FOREIGN_LINEAGE`;
  - (c) positive control: a matching tally → must reach "Database is at head".

  Until that revision, only (a) applies.
- **The cache records which policy passed** *(T3-4)*. It is keyed by image ID **plus
  the floor-policy revision**, so raising the floor invalidates every earlier pass, and
  the next `up` re-probes. No old "yes" carries over.
- **No override flag.** The wrapper may refuse, never permit.
- **Below the floor** is an explicit offline procedure, documented in the rollback
  runbook.
- **Named bypass:** a hand `docker run`, or a bare compose with the pin exported (#5186),
  never meets the wrapper. Under T3-2, an image at or above the floor still refuses to
  *serve* without its witness in production, so the bypass loses the arming dodge and
  keeps only the "run an old image by hand" dodge.

## The tally: checked inside the image

**What it holds:** `witness/volume.tally` in the deploy dir: the lineage (the
sentinel's full content), the db path, and the box's `island_id`. `CreatedAt` is
recorded for forensics only, never compared.

**The sentinel names its island, from now on** *(T3-5)*. New sentinels are written as
`<timestamp> <island_id>`. Existing sentinels keep their timestamp-only content, because
`_write_sentinel` never rewrites. The lineage comparison is the full string either way.

**Arming** *(T3-2, 4/4)*:
- In **production** (`ENVIRONMENT=production`, the default when unset), a `WITNESS_DIR`
  that is missing, empty or unreadable **refuses** (`MIGRATE_REFUSE_UNWITNESSED`). That's
  the same posture as `jwt_secret` and `island_signing_seed`. Unset is never permission.
- The only way to disarm is an explicit `WITNESS_DIR=disarmed`, honoured **only** when
  `ENVIRONMENT` is a development value. Dev, tests and the probe's case (a) use it
  deliberately.
- The tally file is opened with `O_NOFOLLOW`; a symlink refuses.

**The predicate**, armed, before any connect:
- the tally is missing or unreadable → refuse (`MIGRATE_REFUSE_UNENROLLED`);
- **no sqlite file** (`DB_URL` points at `:memory:` or a non-file backend, so `_db_file()`
  returns None) → refuse (Tesla);
- the db path being opened ≠ the tally's path → refuse;
- sentinel present and ≠ the tally's lineage → refuse (`MIGRATE_REFUSE_FOREIGN_LINEAGE`);
- sentinel absent → refuse, with "run `compose.sh enroll`";
- otherwise, today's checks unchanged (lived evidence, `users`, `mode=rw`).

**The wrapper has no arbitrary `run` passthrough** *(T3-3, Carnot)*. Three families
found flags the v3 refusal list missed (`--env`, `--env-from-file`, `--image`,
`--build`, `-f`, `-v` over `/witness`, `/app` or `/data`). A list over Compose's
`run` interface can't be finished, so v4 doesn't keep one. For the gateway service,
`compose.sh` exposes **only named maintenance operations**:
- `enroll`;
- `backup` (`update.sh`'s step 1);
- `check` (read-only: lineage, island mark, `integrity_check`);
- `shell` (an interactive shell with the db mounted **read-only** and no network).

Anything unconstrained is the named offline bypass. With T3-1 and T3-2 in place, a
container that tries to serve un-armed refuses by itself anyway; the allow-list is what
keeps maintenance from *needing* a bypass.

### Enrolment: inspect, classify, confirm, then mutate *(T3-5)*

`compose.sh enroll`, with the stack down. Its steps are fixed, and nothing is written
before step 4:

1. **Inspect, read-only.** A no-network, no-entrypoint helper mounts the volume
   read-only and reads: the sentinel; the worker lock; whether `aiko.db` exists, its
   size, and whether it has a `users` table; any `-journal`/`-wal`/`-shm`; the existing
   tally, if any; and the **island mark**: the first five usernames by creation, the
   user count, and the newest message time.
2. **Classify,** using the migrator's own `_lived_evidence` + `users` logic, imported,
   not re-written. Every state not listed below refuses.
3. **Confirm.** Print the island mark and the planned change. Accept
   `--yes-this-is-<island_id>` only when it equals this box's `island_id` **and**, if
   the sentinel names an island, that name too. For a timestamp-only sentinel (both live
   volumes today) the operator has only the island mark to judge by, and the prompt
   says so.
4. **Mutate, if the row allows it:** migrate-only via an explicit
   `python -m aiko_gateway.migrate --enroll` (an argv mode that skips only the tally
   check, never inferred from a missing variable, and **authorised only by the ceremony
   record**, *v5*), then write the tally atomically (tmp, fsync, rename, fsync the
   directory).

| Inspected state | Row | Confirmation |
|---|---|---|
| sentinel present, db has `users`, no tally | write the tally from the sentinel (no migrate) | `--yes-this-is-<id>` |
| sentinel present, tally present and equal | no-op ("already enrolled") | — |
| sentinel ≠ tally | refuse, except `--replace-lineage <id>`, which prints both lineages | separate flag |
| **tally present, sentinel absent** | refuse, except `--replace-lineage <id>` (a db-only restore into a new volume, or a wiped volume) | separate flag |
| no sentinel, lock and a db with `users`, no tally | `--enroll` migrate-only (today's code adopts: the lock counts as evidence, the db is present, `users` is present), then write the tally | `--yes-this-is-<id>` |
| no sentinel, no lock, a db with `users`, no tally | `--enroll` migrate-only (managed or adopt path), then write the tally | `--yes-this-is-<id>` |
| a lock with no usable db (missing, 0-byte, no `users`) | **refuse**; never offer "delete the lock" | — |
| empty volume, no tally | `--enroll` provisions a fresh island, then write the tally | `--new-island <id>`; refused while any tally exists |
| any `-journal`/`-wal`/`-shm` beside the db | **refuse** until it's resolved (a hot journal would be replayed while the tally check is off) | — |
| anything else | refuse | — |

The `enroll` design holds one more property: **a crash at any point leaves no tally or
the old one**, so the next start refuses and `enroll` runs again idempotently.
`standup.sh` uses the empty-volume row and never offers itself on a volume that has
witnesses.

### `--enroll` needs evidence, not absence *(v5)*

**Why this exists.** PR#404 ran three cage-match rounds, all four seats each round, and
was capped with findings at 11 → 7 → 5+ while the diff grew 448 → 664 → 883 lines. One
class came back one layer deeper every round: **absence read as permission**. First an
unset `WITNESS_DIR`, then `exists()`/`lexists()`/`Path.exists()` answering False when
they *couldn't look* (measured), then a set `WITNESS_DIR` with **nothing mounted** (r3,
Carnot + Tesla independently). In v4, `--enroll` is allowed *because the tally is
missing*. A rule like that can't be finished by patching: the ways to make a file
invisible are open-ended, and each patch closes one.

**The flip.** `--enroll`'s permission comes from a record that **is there**: a one-shot
`witness/enrolling` record, written by the host ceremony after it has inspected,
classified and had the operator confirm. Every way of failing to look (unmounted,
unreadable, wrong directory, wrong variable) now lands on *record missing*, and record
missing **refuses**. The list of ways to be invisible no longer matters, because
invisible is the safe answer.

**Who writes and who removes: the host, both.** The container mounts `/witness`
read-only (v4, unchanged), so it can't remove anything there, and it must not be able
to. The host ceremony already writes the tally; it removes the record in the same
place. The container only **reads** the record and reports which one it acted on.

**The record** is JSON with a fixed set of keys, **every value a string** (read the same
way as the tally, below):

| key | value | checked by the image against |
|---|---|---|
| `v` | `"1"` | exact |
| `row` | `"adopt"` (the two "no sentinel, a db with `users`" rows) or `"new-island"` (the empty-volume row) | the observation in `expect` |
| `island_id` | the confirmed island id | `self_island_id(settings)` |
| `db_path` | the db file as the container sees it | `_db_file()` |
| `expect` | `"sha256:<hex>"` of `aiko.db` as inspected (adopt), or `"empty"` (new-island) | the volume as the image sees it now |
| `expires_at` | RFC 3339 UTC, written as *now + 5 min* | the container's clock (the host's kernel clock) |
| `nonce` | 32 random hex chars | echoed in the log, never compared |

The "sentinel present, db has `users`, no tally" row writes the tally
straight from the sentinel and never runs `--enroll`, so it never writes a record.
`--replace-lineage` moves the old tally aside, then joins whichever row the volume now
classifies as.

**Why `expect` binds the bytes, not the shape.** A record that said only "a db with
`users`" or "empty" would authorise *any* volume of that shape for as long as it lay
there. `"empty"` is the sharp case: a `new-island` record left behind by an aborted
ceremony would bless the next recreated-empty volume, which is exactly S2, the thing
design 20 exists to refuse. So an adopt record carries the **hash of the db the operator
saw the island mark of**. Hashing is cheap: it happens once, at enrolment, with the
stack down. A `new-island` record can't carry a hash, so its defence is time: a short
expiry, and the host removing it.

**What `--enroll` checks, in order.** It refuses at the first failure, and nothing
connects before step 7:

1. **Armed, in every environment.** `--enroll` has no disarmed mode: with nothing to
   enrol into, it has nothing to do. An unset or `disarmed` `WITNESS_DIR` refuses
   (`MIGRATE_REFUSE_UNWITNESSED`).
2. **The island has a name**: `self_island_id(settings)` is non-empty (the shared rule
   below).
3. **There's no tally.** If `_present(tally)` says yes, refuse (`MIGRATE_REFUSE_ENROLLED`,
   unchanged). Here presence *refuses*, which is the safe direction for a missing answer:
   when the directory is unmounted, this step passes and step 4 catches it.
4. **The record.** Read with `_read_text_nofollow`, so it can't be a symlink, FIFO,
   device, oversized or non-UTF-8. `FileNotFoundError` →
   **`MIGRATE_REFUSE_ENROLL_UNAUTHORISED`** ("no ceremony record: run
   `deploy/compose.sh enroll`"). An environmental error → `_unreadable`. Malformed JSON,
   a missing or extra key, or any non-string value → `UNAUTHORISED`.
5. **Not expired, and not far-dated**: `now < expires_at <= now + 10 min`. The upper
   bound means a hand-written record with a far-future date can't become standing
   permission.
6. **It names this container**: `island_id` and `db_path` match exactly.
7. **It names this volume.** For `adopt`, the sentinel is absent and `sha256(aiko.db)`
   equals `expect`. For `new-island`, `aiko.db`, the sentinel and the worker lock are all
   absent (by `_present`, so "can't look" refuses as unreadable).
8. Log `enrolling under ceremony <nonce>`, then the existing migrate path, unchanged.

Step 7's absence checks don't bring the old smell back. Absence there is a
**precondition** the operator has already confirmed, not the source of permission. The
permission came from step 4, and "couldn't look" still refuses.

**What the host ceremony does around the run**, all under the wrapper's lock:
- **Before inspecting:** if `witness/enrolling` exists, remove it, and say that an
  earlier ceremony left one. A record is only ever made fresh, by this run.
- **After confirming:** write the record atomically (unique temp file, fsync, rename,
  fsync the directory), then run the container with `--enroll`.
- **On success:** exit 0 **and** a log line naming *this* ceremony's nonce. Only then
  read the sentinel the run just wrote (read-only helper, the same reader as inspect;
  the run is what makes the lineage, so the tally can't be prepared in advance) and
  write the tally from it (adding an `enrolled_by` nonce for forensics, never compared), then
  remove the record.
- **On any failure:** remove the record (`finally`).

**Crash table.** Every interleaving falls into one of these four outcomes:

| crash after… | what's left | next start | next `enroll` |
|---|---|---|---|
| record written, before the run | record, no tally | refuses: unenrolled | removes the record, starts over |
| the run, before the tally | record, migrated db **+ the sentinel the run wrote**, no tally | refuses: unenrolled | removes the record; the volume now classifies as "sentinel present, db has `users`, no tally", so it writes the tally from that sentinel, with no second `--enroll` (and the stale record's hash would no longer match anyway) |
| the tally, before removing the record | tally + record | **serves** (the predicate never reads the record) | "already enrolled"; removes the leftover |
| removing the record | tally | serves | "already enrolled" |

A leftover record **next to a tally is inert**: step 3 refuses `--enroll` before step 4
ever reads it.

**The trust limit, named.** Anyone who can write the deploy dir's `witness/` can forge a
record. But they can forge the tally just as easily, so the record adds no new trust
root. It sits under the one the tally already depends on: the operator's box. What the
record adds is that **forgetting** or **failing to look** can't enrol anything.
Fixing that was the job.

### Round-3 folds *(v5)*

The four other real findings from PR#404 r3. Each fix removes the shape that made the
bug possible, rather than guarding against it.

- **The sentinel uses a unique temp file** *(Carnot)*. It's created `O_EXCL` under a
  random name in the db's directory, then fsynced and renamed. The fixed `.tmp` name and
  its "remove the leftover first" fence are deleted, not hardened. A crash's orphan temp
  file is inert, because nothing reads that name. The same writer goes into the Dart
  ceremony for the record and the tally.
- **Advice comes from what was seen, not what was configured** *(Tesla)*. The
  missing-db remedy names `--replace-lineage` only when a tally was **actually read**.
  If the witness couldn't be looked at, the message says so and gives no remedy that
  would re-bless. `refuse_missing_db_message` takes what was observed as a parameter
  instead of re-reading `WITNESS_DIR`.
- **The tally's fields are type-checked, not stringified** *(Tesla)*. `str(None)` is
  `"None"`, which passes an emptiness check and then lands in the FOREIGN voice, whose
  remedy re-blesses. Every field must be a JSON string (`isinstance(v, str)`), or the
  read refuses as malformed (UNENROLLED). The record uses the same reader.
- **One rule for an empty island id** *(Noether)*. Armed, an empty
  `self_island_id(settings)` refuses before anything else, in **both** modes
  (`MIGRATE_REFUSE_UNNAMED`). So the sentinel writer can no longer write a nameless
  sentinel that the tally reader would then reject forever. Unarmed (dev/tests), the
  legacy timestamp-only sentinel stays, and no tally reader runs there.

Known release gate (r3 #6, Noether): every remedy names `compose.sh enroll`, which isn't
in the tree until the `deploy/` PR. That's why no tag may contain the image PR before
the `deploy/` PR lands. The `refuse_missing_db_message` docstring's claim that no advice
is unworkable has to stay true *at the tag*, not just at the merge.

### Restores keep their lineage

`update.sh`'s backup copies `.aiko.db.initialized` beside `aiko.db`, and the runbook
restores **the pair** with the stack **down**. A restore into the same volume keeps its
lineage, and the tally matches. A db-only restore into a new volume needs
`--replace-lineage`. Runbook line: *after restoring, remove any `aiko.db-journal` beside
the restored file*. `enroll` refuses until that's done.

### Durability

`witness/` joins `image.env` as a **box-local, never-synced** path, under the same rule
and test. Losing it makes every start refuse in production. That's loud, and `enroll`
recovers it.

## Q3: states × transitions

**Transitions:**
- **create**: a new container, through the wrapper's create-inspect-start;
- **reuse**: an existing container started through the wrapper (inspected first);
- **restart**: `restart: always`, a daemon restart, a bare `docker start`; no wrapper;
- **bypass**: a hand `docker run`, or a bare compose with the pin exported.

| Volume state | create | reuse | restart | bypass |
|---|---|---|---|---|
| **S1** right volume, intact | serves | serves | serves | out of model |
| **S2** recreated empty | refused: lineage predicate | n/a: a recreate needs every container removed (meas. 2) | n/a, same | out of model |
| **S3** foreign lineage (a foreign *pair*) | refused | refused | **refused** | out of model |
| **S3b** foreign db under the local sentinel | **not detected** (named limit) | same | same | out of model |
| **S4** stale copy of this lineage | serves, by design | serves | serves | out of model |
| **S5a** db missing / 0-byte / no `users` (witnesses intact) | refused | refused | refused | out of model |
| **S5b** whole volume wiped in place | refused (sentinel absent, tally present) | refused | **refused** | out of model |
| **S5c** truncated, `users` still readable | **not detected** until Q4's `quick_check` | same | same | out of model |
| **S6** another db path, mount or non-file backend | refused (path ≠ tally / no sqlite file) | refused | refused | out of model |
| **S7** tally lost | refused (unenrolled) | refused | refused | out of model |
| **S8** container created un-armed (no `WITNESS_DIR`/mount) | refused by the wrapper's inspect step, and by the image in production | refused | **refused: the image, in production** | an image at or above the floor refuses in production |

On the watcher, a refusal that loops under `restart: always` is a **host-class failure**,
not a bad release (design 18's "host failed" state). So S5, S7 and S8 must never
quarantine a good digest. That handling is part of what gets built.

## Q1, Q2, Q4, Q6

- **Q2:** keep the per-directory lock as a production feature (it catches a *fresh* file
  beside an island). The tally's path field now also catches a *copied* island at
  another path.
- **Q1:** keep the lock as a witness, renamed **the directory witness**. Revisit after
  the tally ships.
- **Q4:** `quick_check` gets its own PR, timed on both volumes. S5c is honestly not
  covered until then.
- **Q6:** belongs to #393.

## What gets built

1. **Image PR** (cage-match: trust boundary; **the next independent strike on v4**):
   - one lineage predicate, called from `migrate.run()` and lifespan's
     `verify_schema()`;
   - production arming (`MIGRATE_REFUSE_UNWITNESSED`), the explicit dev disarm, and
     `O_NOFOLLOW`;
   - the new refusal codes;
   - `--enroll` mode, authorised only by the ceremony record (v5: steps 1–8,
     `MIGRATE_REFUSE_ENROLL_UNAUTHORISED`, `MIGRATE_REFUSE_UNNAMED`);
   - the sentinel now names its island, written through a unique temp file (v5);
   - the round-3 folds (v5);
   - tests for every state in the Q3 matrix, plus a test that boots the app **without**
     the migrator against a mismatched tally and expects a refusal.
2. **`deploy/` PR**:
   - create-inspect-start with the policy-keyed probe cache;
   - the three-case probe;
   - the gateway's maintenance allow-list in place of `run` passthrough;
   - `enroll` with the island mark, the db hash and the ceremony record (write, run,
     check the nonce, tally, remove), in Dart; slice it so the read-only
     inspect-and-classify half ships first;
   - the `witness/` mount in compose;
   - backups carrying the sentinel;
   - runbook changes.
3. **Watcher change** (design 18 (b)): treat `MIGRATE_REFUSE_UNENROLLED`,
   `UNWITNESSED` and `MISSING_DB` as host-class failures; never quarantine on them.
4. **Rollout, per box, as one maintenance step** *(T3-6)*:
   1. pause the watcher;
   2. `compose.sh down`;
   3. sync compose + `deploy/` from the tag (together with #5186 and design 18 (b): one
      hand-sync);
   4. set `image.env`;
   5. `compose.sh enroll --yes-this-is-<id>` (row 1, checked against the sentinels
      measured above);
   6. `compose.sh up`;
   7. verify `/health`;
   8. re-arm the watcher.

   There is no recreate before `enroll`. The update script's drift preflight already
   refuses step 4 without step 3. Raising the floor (probe cases b and c) is a separate,
   later change, after both islands are enrolled.
