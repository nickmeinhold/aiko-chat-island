# Design 20: what a volume can witness about itself

Answers [#400](https://github.com/nickmeinhold/aiko-chat-island/issues/400) Q1–Q3 and
Q7, with Q4–Q5 noted where the measurements reached them. Status: **v2, PROPOSED**.
v1 was struck by four families and came back **RECAST 4/4**
([`20-TEMPER.md`](20-TEMPER.md)). This version folds that strike. Each fold is marked
*(T1-n)*, where n is the flaw's number in the TEMPER synthesis.

## The question

The volume sentinel (#399, v0.17.1) stops a lived volume from silently becoming a new,
empty island. A record on a volume can say one thing: *an island has lived here*. It
cannot say *this is the volume that should be here*, because from inside the container
a fresh volume and a first standup look the same, byte for byte.

The answer, narrowed by the strike: **a record inside the volume proves the volume has a
past. Only a record outside it can prove the volume belongs to this host's island, and
then only its _lineage_, not how recent it is** *(T1-4)*.

## The rule that organises v2: check each fact where it is fixed

The strike's sharpest finding (Tesla, *T1-1*): **the image a container runs is fixed
when the container is created. Whether its database exists is true or false each time
it starts.** `restart: always`, a daemon restart or `docker start` re-run an existing
container with no wrapper in the path. So:

- a host check that runs before `up` can guard **only facts fixed at creation**: which
  image, which volume object;
- a fact that can change between starts (is the db there, does it have tables) can only
  be guarded by **the image itself**, because that's the only code that runs on every
  start.

v1 put a start-time rule (sentinel present, db absent) into a creation-time door, and
three families found the gap independently. v2 puts each check where its fact is fixed.

| Fact | Fixed when | Guarded by |
|---|---|---|
| the image contains the volume guard | container creation (the image can't change under a container) | **the image floor**, in the wrapper (§Q7) |
| which volume object is mounted | container creation (`volume rm` refuses while any container references it, measured below) | **the tally**, in the wrapper (§Q3) |
| the db exists and holds an island | every start | **the migrator**, in the image, on every start (existing, unchanged) |

## What was measured

All on 2026-10-24. Read-only on both live islands, plus throwaway probes on syd.

| | syd (`chat.imagineering.cc`) | melb (`chat.enspyr.co`) |
|---|---|---|
| `.aiko.db.initialized` content | `2026-10-20T06:01:11.810364+00:00` | `2026-10-20T06:02:12.263441+00:00` |
| Docker volume `aiko_data` `CreatedAt` | `2026-07-05T00:17:38Z` | `2026-07-11T01:28:53Z` |
| `.aiko-worker.lock` | 0 B, 2026-08-02 22:09 | 0 B, 2026-08-02 22:36 |
| `-wal` / `-shm` / `-journal` beside `aiko.db` | none | none |
| `backups/` in the deploy dir | 38 entries | 39 entries |

Behaviours, each with a control:
1. **A missing `external: true` volume makes Compose refuse** (`external volume ... not
   found`, exit 1, nothing created). With the volume present, the same probe started.
2. **`docker volume rm` refuses while any container references the volume, including a
   stopped one, and `-f` doesn't override it** (`volume is in use`, exit 1 both ways).
   With the container removed, the same `rm` succeeded. So replacing the volume object
   requires removing the containers, and the next container has to be *created*, which
   means `up` and therefore the wrapper.
3. **Images carry no OCI labels** (`Config.Labels` is `null`). Every release image does
   carry `/app/build-info.json` (`git_sha`, `ref`, `built_at`), readable with
   `docker run --rm --entrypoint cat`, and the entrypoint never runs.
4. **The guard can be detected in an image by a probe, across all 18 release images on
   syd** (v0.9.3 → v0.17.2): `grep -c MIGRATE_REFUSE_MISSING_DB
   /app/src/aiko_gateway/migrate.py` returns `0` for every release up to v0.17.0, and
   `2` for v0.17.1 and v0.17.2. There are no false positives or negatives across that
   history.

## Q7: the image floor *(T1-1, Nick 2026-10-24: "image floor")*

Images built before v0.17.1 ignore the sentinel. On a lived volume whose db is missing,
one of them builds a fresh, empty island. v1 tried to re-check the db from the wrapper.
That copy was narrower than the migrator's rule (it would pass a 0-byte file, a
bookkeeping-only db and a lock-only volume), and it ran only at creation. **Deleted.**

**The rule: the wrapper refuses to create a container from an image that doesn't
contain the volume guard.** Nothing in production runs below v0.17.1, so every serving
start runs the migrator's single predicate (`_lived_evidence` + the `users` check +
`mode=rw`), on every start, including restarts. There is no second copy of the rule.

- **The probe is for the capability, not a version.** `docker run --rm --entrypoint sh
  <image> -c 'grep -q MIGRATE_REFUSE_MISSING_DB /app/src/aiko_gateway/migrate.py'`. It
  needs no version ordering, it works on a digest pin (where there's no tag to parse)
  and on a local build with an empty `ref`, and measurement 4 shows it separating the
  real history exactly.
- **The probe string becomes a contract.** A test in this repo asserts that
  `MIGRATE_REFUSE_MISSING_DB` appears in `migrate.py`'s refusal path. Renaming it then
  fails CI instead of silently disabling the floor. It's already an operator-facing
  refusal code, so it's a stable name anyway.
- **It runs at the doors that create containers:** `compose.sh up`, `create` and `run`,
  for the gateway service, with the image resolved for this invocation. It refuses with
  the image ref and the reason. There is **no override flag**: the wrapper may refuse,
  never permit (Maxwell's union-of-refusals invariant, the part of it that survived).
- **The watcher.** TEMPER-18 fold #9 already says the watcher refuses an auto-restore
  to a pre-guard image. It goes through the same wrapper, so it's one rule at one door
  for both the human and the watcher.
- **Below the floor** is an explicit offline procedure: a fresh island, or forensic
  work on a copy, run by hand without `compose.sh`. It's documented as such in the
  rollback runbook, which will replace the "keep the current compose" note for targets
  below v0.17.1.
- **Bypass, named.** `docker run` by hand, or a bare compose with an exported pin
  (#5186), never meets the wrapper. That's the same class as #401's limit, closed by the
  same future work, and not claimed here.

Side benefit: the same `--entrypoint cat /app/build-info.json` helper gives the drift
guard a ref for a **digest** pin, which `island_ref_from_images` can't get today (a
listed input to design 18 step b). One helper, two uses.

## Q3: doors, as states × transitions *(T1-6)*

v1 listed four doors read off the compose file. The strike showed they mixed causes
with states and missed transitions. This version crosses **what the volume holds** with
**how a container gets to run**, and marks each cell.

**Volume states:**
- **S1** the right volume, intact;
- **S2** a recreated empty volume;
- **S3** another island's lineage;
- **S4** a stale copy of *this* lineage;
- **S5** the right volume with the db missing, 0-byte, truncated or bookkeeping-only;
- **S6** a different db path or mount (`DB_URL`, `-v`, `-f`);
- **S7** the host-side tally lost.

**Transitions:**
- **create**: `compose.sh up`, `create` or `run` (the wrapper runs);
- **restart**: `restart: always`, a daemon restart, `docker start` (no wrapper; same image
  and same volume object as at creation);
- **bypass**: a hand `docker run`, or a bare compose with the pin exported.

| | create (wrapper) | restart (no wrapper) | bypass |
|---|---|---|---|
| **S1** intact | serves | serves | out of model |
| **S2** recreated empty | **refused: tally** (sentinel absent) | impossible: a recreate needs every container removed (meas. 2), so the next run is a create | out of model |
| **S3** foreign lineage | **refused: tally** (sentinel differs) | only by replacing a mounted volume's contents in place: deliberate, out of model | out of model |
| **S4** stale same lineage | **serves, by design**: indistinguishable from a legitimate restore *(T1-4)* | serves, by design | out of model |
| **S5** db damaged | **refused: migrator**, after the floor guarantees the image has it | **refused: migrator** (every start) | out of model |
| **S6** other path/mount | **refused: tally** bound to the effective mount *(T1-3)*; a moved `DB_URL` inside `/data` is also caught by the lock | n/a: mount fixed at creation | out of model |
| **S7** tally lost | **refused** *(T1-2)* | serves (it was right at creation) | out of model |

"Out of model" means one specific thing here: actions that don't go through the
operator's doors. It's the same boundary as #401's pin and #5186's future hardening.
S4 is the honest limit: **the tally proves lineage, never generation.** Detecting
freshness would need an external record of the island's progress, kept in step
through every restore, and that record would refuse exactly the restores the runbook
prescribes.

## The tally, v2

**What it records:** `volume.tally` in the deploy dir, one line:
`<volume name> <sentinel content>`.

`CreatedAt` is **out of the refusal path** *(T1-5)*. Its only distinct row was "copied
into a new volume object", which is a legitimate move. It's also an unmeasured clock:
a format change in a Docker upgrade would fire on both islands the same night and teach
operators to reach for re-adopt. It is recorded beside the tally for forensics and
never compared.

**Which volume it checks** *(T1-3)*: the gateway service's `/data` mount and `DB_URL`,
resolved from `docker compose config` *for this invocation*, never a hardcoded
`aiko_data`. A `run -e DB_URL=…`, a `-v` or an `-f` that points the gateway anywhere
other than the enrolled volume and path refuses.

**How it's read:** a helper container with no entrypoint, a read-only mount and no
network (`docker run --rm --network none --entrypoint cat -v <vol>:/data:ro <guard
image> /data/.aiko.db.initialized`). Unreadable means refuse. The helper must never run
the app's entrypoint, which would run `migrate.run()` (Noether).

**Its lifecycle** *(T1-2)*: a missing tally refuses, the way a missing `image.env`
refuses.

| Tally | Volume | Action |
|---|---|---|
| present, matches | lineage matches | proceed |
| present | sentinel differs or is absent | **refuse**; print `adopt-volume` |
| absent | anything | **refuse**; print `adopt-volume` (or `standup`, for a first island) |
| `pending` | no evidence yet | first standup in progress: proceed, and complete the tally once the sentinel is visible |

- **`adopt-volume`** is an explicit subcommand. It runs with the stack down, shows the
  live sentinel and `CreatedAt`, requires `--yes-this-is-<island>`, and writes the tally
  atomically (tmp, fsync, rename, fsync the directory: the same discipline as
  `_write_sentinel`). Syd and melb are enrolled this way at the hand-sync that ships
  this. Nothing writes a tally from whatever happens to be mounted.
- **A first standup** writes `pending` before `up`, so a crash before the sentinel
  exists leaves `pending`, not an absent tally (Tesla's protocol). The tally is
  completed only once the sentinel is visible through the helper.
- **Durability:** design 18's watcher and any refresh of the deploy dir must keep
  `volume.tally`, or fail closed. Losing it refuses, which is loud.
- **Rehearsal** (design 18 phase B) uses a side directory, not `aiko_data`. It must
  never read or write the production tally. The rehearsal path is outside the wrapper's
  serving doors, and design 18 (b) owns its own witness.

## Q2: the per-directory lock is a production feature

Noether r3 (#399): a directory that hosted db A refuses a fresh db B beside it while the
lock exists. In dev that's a nuisance. In prod it guards S6 from inside the image: a
`DB_URL` moved to `/data/aiko2.db` finds the lock and refuses. **Keep it.**

## Q1: keep the lock as a witness; rename what it does

Its *bridge* role is over: both live volumes carry a sentinel. But the same `exists()`
check also does Q2's job. Keep the check, and change the docstrings and the refusal
text to call the lock **the directory witness**. Revisit once the tally, bound to the
effective `DB_URL`, covers S6 from the host.

**Named tradeoff** *(T1-7)*: a volume carrying only the lock (no sentinel) can't be
enrolled: `adopt-volume` refuses it, with "boot an image at or above the floor once,
then adopt". No live volume is in this state (measured). Owner: the `deploy/` PR.

## Q4, Q5, Q6

- **Q5:** the real risk is a hot `aiko.db-journal` (journal mode `DELETE`; WAL is off),
  not WAL. Runbook line: *after restoring, remove any `aiko.db-journal` beside the
  restored file*. None exists on either volume (measured).
- **Q4:** `PRAGMA quick_check` before `upgrade head` on a lived volume. It's unmeasured,
  and proposed as its own PR with timings on both volumes. (S5's truncated-file row
  depends on it for files that still have `users`.)
- **Q6:** belongs to #393.

## What gets built (after a clean round 2)

One `deploy/` PR, landing in the same hand-sync as #5186 and design 18 (b):
1. The floor probe at the container-creating doors, plus the contract test for
   `MIGRATE_REFUSE_MISSING_DB`.
2. `volume.tally` with `adopt-volume`, `pending`, mount binding and a no-entrypoint
   reader.
3. The `build-info.json` reader, shared with the drift guard.
4. The runbook changes: below-floor recovery, the hot journal, enrollment at the
   hand-sync.

Plus one docstring PR for Q1's rename.

## Questions for round 2

1. Does the create/restart split hold everywhere? Is there a path where a container's
   *image* changes without a create (a `docker commit`, or the tag being moved under a
   running container)?
2. Is `docker compose config` the true effective model for every `compose.sh`
   subcommand, including `run` with `-e` and `-v`?
3. Is a grep for a refusal code too weak a capability probe? The alternative is a
   dedicated marker file, which every pre-marker image would lack, so it needs this grep
   as its bootstrap anyway.
