# Design 20: what a volume can witness about itself

Answers [#400](https://github.com/nickmeinhold/aiko-chat-island/issues/400) Q1–Q3 and
Q7, with Q4–Q5 noted where the measurements reached them.

**Status: v5.1 = v4 + the ceremony addendum** ([§ `--enroll` needs evidence, not
absence](#--enroll-needs-evidence-not-absence-v5), from PR#404's capped cage-match,
[ct#5189](https://github.com/enspyrco/claude-tasks/issues/5189)). The addendum was struck
once (A1: RECAST, 4/4 seated) and its nine findings are folded in as v5.1. **The v5.1
folds are UN-TEMPERED** until round A2 lands in [`20-TEMPER.md`](20-TEMPER.md).

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
7. **The live dbs are small, and hashing them is free** *(v5.1, A1)*. `aiko.db` is
   393,216 B on syd and 569,344 B on melb, and `sha256sum` takes ~0 ms on each. Every
   file in `aiko_data` is `root:root`, so the host writes the volume ticket through a
   helper container, not as the deploy user. The volumes also hold stray hand backups
   (`aiko.db.pre-*`, `aiko.pre-0024-*.db`): classify ignores names it doesn't own, and
   they never count as evidence.

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

  Until that revision, only (a) applies. **Except `--enroll`** *(v5.1, A1: Maxwell,
  Tesla)*. Its cases ship **with** the `deploy/` enroll, not later, because a v4-shaped
  image enrols on absence and the floor would otherwise still pass it. The probe runs
  `--enroll` against its own throwaway witness dir:
  - (d) no record → must refuse with `MIGRATE_REFUSE_ENROLL_UNAUTHORISED`, nothing
    created;
  - (e) a valid new-island record but no volume ticket → must refuse, nothing created;
  - (f) a valid adopt record with a `-wal` sibling → must refuse without connecting.
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
   check, never inferred from a missing variable, and **authorised only by the
   two-half token: witness record + volume ticket**, *v5.1*), then write the tally atomically (tmp, fsync, rename, fsync the
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

**The flip.** `--enroll`'s permission comes from evidence that **is there**, written by
the host ceremony after it has inspected, classified and had the operator confirm (v5:
one record; v5.1: two matching halves, below). Every way of failing to look (unmounted,
unreadable, wrong directory, wrong variable) now lands on *record missing*, and record
missing **refuses**. The list of ways to be invisible no longer matters, because
invisible is the safe answer.

**v5.1: the permission comes in two halves** *(A1, 3 RECAST + 1 DISSOLVE; see
[`20-TEMPER.md`](20-TEMPER.md))*. v5 used one record. A1 showed one record isn't enough:
on the new-island row it had nothing to bind but `"empty"`, and an unmounted data
directory *reads* as empty (Tesla). That's the capped class again, one layer down. A1
also showed the image couldn't spend a read-only record, so it wasn't one-shot (Carnot).
v5.1 splits the permission into two parts that must match, like the two halves of a
medieval tally stick. Each part lives where the other side can't forge it:

- **the witness record**, `witness/enrolling`, is on the host, mounted **read-only** into
  the container. The container can read it but not mint it. Reading it also proves the
  container sees the directory the host wrote to. That **mount proof** is what closes
  r3's hole: the container can't see the host's directory, and nothing outside the
  container can check that for it (Maxwell).
- **the volume ticket**, `.aiko-enroll-ticket`, is on the volume, beside the db. The
  host writes it after confirmation (through a helper container: the volume's files are
  root-owned, measured). It's the evidence that *this* volume is the one the operator
  was shown, and the volume is read-write, so the container can **claim** it, once.

Neither half on its own does anything. A recreated or wiped volume has no ticket, so S2
is refused by construction, not by a clock.

**The record** is JSON with a fixed set of keys, **every value a string** (the tally's
reader, below):

| key | value | checked by the image against |
|---|---|---|
| `v` | `"1"` | exact |
| `row` | `"adopt"` (the two "no sentinel, a db with `users`" rows) or `"new-island"` (the empty-volume row) | the row the image then enforces (step 7) |
| `island_id` | the confirmed island id | `self_island_id(settings)` |
| `db_path` | the db file as the container sees it | `_db_file()` |
| `expect` | `"sha256:<hex>"` of `aiko.db` as inspected (adopt), or `"absent"` (new-island) | the db, read through the held directory (step 6) |
| `ticket` | 32 random hex chars | the volume ticket's content, exactly |
| `replaces` | the current tally's lineage, or `""` for a first enrolment | the tally (step 4) |
| `expires_at` | RFC 3339 UTC, written as *now + 5 min* | the container's clock (the host's kernel clock) |

`expires_at` is **cleanup, not identity**. The ticket identifies the volume. Expiry only
stops one forgotten file from becoming a standing key: `now < expires_at <= now + 10
min`, checked again after the hash.

The "sentinel present, db has `users`" row (first enrolment, or a `--replace-lineage`
to it) writes the tally straight from the sentinel and never runs `--enroll`, so it
writes no record and no ticket. **The old tally is never moved aside.** A replacement is
one atomic rename over it, made at the end. A crash before that leaves the old tally,
and the retry still needs `--replace-lineage` (Carnot).

**What `--enroll` checks, in order.** It refuses at the first failure, and nothing
connects before step 8:

1. **Armed, in every environment.** `--enroll` has no disarmed mode: with nothing to
   enrol into, it has nothing to do. An unset or `disarmed` `WITNESS_DIR` refuses
   (`MIGRATE_REFUSE_UNWITNESSED`).
2. **The island has a name**: `self_island_id(settings)` is non-empty (the shared rule
   below).
3. **The record: the mount proof comes first.** Read with `_read_text_nofollow`, so it
   can't be a symlink, FIFO, device, oversized or non-UTF-8. `FileNotFoundError` →
   **`MIGRATE_REFUSE_ENROLL_UNAUTHORISED`**. An environmental error → `_unreadable`.
   Malformed JSON, a missing or extra key, or any non-string value → `UNAUTHORISED`. Then
   `expires_at` is checked, and `island_id` and `db_path` must match this container.
   These last two are host-against-host: they catch drift in the ceremony's config, not
   a hostile volume.
4. **The tally, against `replaces`.** Only now, with the directory proven visible, does
   the tally's state mean anything. `replaces == ""` → the tally must be absent
   (`_present`, and presence refuses with `MIGRATE_REFUSE_ENROLLED`). Otherwise the tally
   must read, typed, with a lineage equal to `replaces`.
5. **Hold the data directory.** `open(db.parent, O_DIRECTORY | O_NOFOLLOW)`. **Any**
   failure, including `ENOENT` (the directory itself missing: an unmounted volume),
   refuses as UNREADABLE. From here on, every name in the volume is reached by `openat`
   on this one fd. So `ENOENT` means "absent from the directory I am holding", never "I
   couldn't look".
6. **The ticket, and the row.** `openat` the ticket (`O_NOFOLLOW`, regular, small):
   `ENOENT` → `UNAUTHORISED` ("no ticket on this volume: it is not the volume the
   ceremony prepared"). Its content must equal `ticket`. Then the row, through the same
   fd:
   - **`new-island`:** `aiko.db`, the sentinel and the worker lock are all `ENOENT`.
     Absence is a fact about a directory proven to be the right one, because the ticket
     is in it.
   - **`adopt`:** the sentinel is `ENOENT`. `aiko.db` is a non-empty regular file whose
     `sha256`, read through the fd, equals `expect`. **No `-wal`, `-shm` or `-journal`
     sibling** (Tesla: the image enforces this, not only the host's classify table,
     because the image is what opens SQLite with the predicate off).
7. **Claim the ticket.** `renameat(ticket → .aiko-enroll-ticket.claimed-<ticket>)`, on
   the held fd. The rename is atomic, so if a second runner raced this one, exactly one
   rename succeeds, and the loser gets `ENOENT` and refuses. Re-check `expires_at` (the
   hash took time). Log `enrolling under ceremony <ticket>`.
8. **Migrate with the row pinned, not "unchanged".** `new-island` only provisions: if a
   db exists by now, it refuses and never adopts. `adopt` only adopts or upgrades: if
   the db is missing, it refuses and never provisions. The migrator gets the row as an
   argument; it is not re-derived from what's on disk.

*Named limit:* SQLite opens by path, not by the held fd, so between step 6's hash and
step 8's connect, a path swap isn't excluded by the fd. What excludes it is the ceremony
below: the stack is down, the ticket is claimed, and only the uniquely named enrol
container mounts the volume. A swap in that window needs the bypass, which is out of
model.

**What the host ceremony does**, under the wrapper's lock, **and** with exclusion that
outlives the wrapper:
- **One enrol container, by name.** The run is `create --name aiko-enroll --restart no`.
  The **Docker daemon** enforces unique names, so a second ceremony's `create` fails
  with a name conflict *while the first container exists*, even after its wrapper was
  SIGKILLed and its lock released (Carnot, Tesla, Noether). The ceremony doesn't *check*
  whether the name is free (that would be absence again). It just creates, and a
  conflict refuses, saying: "an enrolment container exists; `docker logs aiko-enroll`,
  wait for it to exit, then `docker rm` it". `compose.sh up` refuses the same way while
  `aiko-enroll` exists.
- **Before writing:** remove any leftover record, ticket or claimed ticket. Only this
  run ever makes them.
- **After confirming:** write the volume ticket (helper; unique temp file, fsync, rename,
  fsync the directory), then the witness record (same writer), then create and start
  `aiko-enroll` with `--enroll`, and wait for it.
- **Success is read off the outcome, not the log** (Tesla, Maxwell). Exit 0, the nonce
  in the log, **and** a read-only helper finds the claimed ticket carrying this nonce
  **and** a sentinel whose content names the confirmed island. Only then: write the
  tally (an atomic rename, over the old one when `replaces` is set), then remove the
  record, the claimed ticket and the container.
- **`ENROLL_UNAUTHORISED` after the host wrote both halves is a mount failure**
  (Maxwell), not "run enroll": the container didn't see what the host wrote. Refuse,
  name the bind source path and the volume, and never retry.
- **On any failure:** remove the record and any unclaimed ticket (`finally`). A
  container that's still running is left for the operator, as above.

**Crash table.** Each row is a crash at that point, with every process dead except where
stated:

| crash after… | what's left | next start | next `enroll` |
|---|---|---|---|
| ticket and/or record written, before the run | ticket, record, no tally | refuses: unenrolled | removes both, starts over |
| **the wrapper dies, the container runs on** | a running `aiko-enroll` | `compose.sh up` refuses (the name exists) | `create` conflicts: refuses until the operator has waited for and removed it |
| the run, before the tally | claimed ticket, record, migrated db + the sentinel the run wrote, no tally | refuses: unenrolled | removes the leftovers; the volume classifies as "sentinel present, db has `users`", so it writes the tally from that sentinel, with no second `--enroll` |
| the tally, before cleanup | tally, record, claimed ticket | **serves** (the predicate reads neither) | "already enrolled"; removes the leftovers |

A leftover record is inert in every row: step 4 refuses it against a tally that doesn't
match `replaces`, and step 6 finds no unclaimed ticket. A claimed ticket is inert
everywhere, because nothing accepts that name.

**The trust limit, named** *(Noether, Tesla)*. Anyone who can write the deploy dir's
`witness/` and the volume can forge both halves. That's the same principal the tally
already trusts (the operator's box), so the design adds no new trust root. It **does**
add a second capability under that principal. The tally lets a start *serve* a lineage;
a record plus a ticket let `--enroll` *mutate* a volume with the lineage predicate
skipped. Name it, so nobody "fixes" a later finding by moving the check back onto the
host. What the split token adds is that **forgetting**, **failing to look**, **a
recreated volume** and **a second runner** can't enrol anything.

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
   - `--enroll` mode, authorised only by the two-half token (v5.1: steps 1–8, the held
     directory fd, the ticket claim, the row pinned into the migrator,
     `MIGRATE_REFUSE_ENROLL_UNAUTHORISED`, `MIGRATE_REFUSE_UNNAMED`);
   - the sentinel now names its island, written through a unique temp file (v5);
   - the round-3 folds (v5);
   - tests for every state in the Q3 matrix, plus a test that boots the app **without**
     the migrator against a mismatched tally and expects a refusal.
2. **`deploy/` PR**:
   - create-inspect-start with the policy-keyed probe cache;
   - the three-case probe;
   - the gateway's maintenance allow-list in place of `run` passthrough;
   - `enroll` with the island mark, the db hash, the volume ticket and the witness
     record, the `aiko-enroll` named container, success read off the outcome, and
     `up` refusing while `aiko-enroll` exists, in Dart; slice it so the read-only
     inspect-and-classify half ships first;
   - probe cases (d)–(f) for `--enroll`;
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
