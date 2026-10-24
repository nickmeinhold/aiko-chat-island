# Design 20: what a volume can witness about itself

Answers [#400](https://github.com/nickmeinhold/aiko-chat-island/issues/400) Q1–Q3 and
Q7, with Q4–Q5 noted where the measurements reached them. Written for `/design-temper`
before any code. Status: **PROPOSED**.

## The question

The volume sentinel (#399, v0.17.1) stops a lived volume from silently becoming a new,
empty island. A record on a volume can say one thing: *an island has lived here*. It
cannot say *this is the volume that should be here*, because a fresh volume and a
first standup look the same byte for byte from inside the container (Tesla, #399 r2/r3).
So the real question is what can witness that, and where it has to live.

The short answer this note argues for: **a record inside the volume proves the volume
has a past. Only a record outside it can prove the volume is the right one. The proof
comes from pairing the two, like a tally stick split in half: each half means little
alone, and they prove something only when they match.**

## What was measured (2026-10-24, both live islands, read-only)

| | syd (`chat.imagineering.cc`) | melb (`chat.enspyr.co`) |
|---|---|---|
| Docker volume `aiko_data` `CreatedAt` | `2026-07-05T00:17:38Z` | `2026-07-11T01:28:53Z` |
| `.aiko.db.initialized` content | `2026-10-20T06:01:11.810364+00:00` | `2026-10-20T06:02:12.263441+00:00` |
| `.aiko-worker.lock` | 0 B, 2026-08-02 22:09 | 0 B, 2026-08-02 22:36 |
| `-wal` / `-shm` / `-journal` beside `aiko.db` | none | none |
| `backups/` in the deploy dir (host, outside the volume) | 38 entries | 39 entries |

And one behaviour, measured on syd with a throwaway project and a positive control:
**a missing `external: true` volume makes Compose refuse** (`external volume
"aiko_probe_q3" not found`, exit 1, no volume created). With the volume present, the
same probe started and exited 0. `aiko_data` is `external: true`
(`docker-compose.yml:373`), so Compose never creates an empty one on its own.

## Q3 first: which doors can hand the gateway an empty volume?

Q3 is the core, and the other answers follow from it. Listed exhaustively from the
compose file and the measurement above, not recalled:

1. **Compose creates one.** Closed: `external: true` refuses (measured above).
2. **`DB_URL` points somewhere else.** It is a literal in the compose `environment:`
   block (`docker-compose.yml:65`), not a `${…}` interpolation, so neither `.env` nor the
   shell can steer it (the compose file has no `env_file:`). Changing it means editing
   the compose file, and the drift guard (#4230) refuses a box whose compose differs
   from the tag's. A deliberate change in the repo is a code review, not an accident.
   And if it moved to another file inside `/data`, the per-directory lock would refuse
   it (see Q2).
3. **A human recreates the volume.** `docker volume rm aiko_data && docker volume
   create aiko_data`, a Docker data-root wipe, or a rebuilt or migrated host where the
   restore step was missed. **This is the one open door.** Nothing in the volume can
   catch it: the volume is genuinely new, and its emptiness is the truth about it.
4. **The wrong lived volume.** Another island's volume, or an old copy of this one,
   restored in its place. The sentinel says "lived", which is true, and the island
   serves someone else's data or an old version of its own. The record inside the
   volume cannot catch this either.

Doors 3 and 4 have the same shape. The volume tells the truth about itself, but it
isn't the volume this host should have. Only something that remembers which volume
belongs here can tell.

### The proposal: a host-side tally

The two values in the table above are already, in effect, identities, and both differ
between the boxes:

- Docker's `CreatedAt` for `aiko_data` changes whenever the volume is recreated
  (door 3).
- The sentinel's content is a timestamp written once in a volume's life and never
  rewritten (`_write_sentinel` returns early if it exists). It travels with the volume
  if the volume is copied, so it identifies *this island's data* (door 4).

So: the deploy dir (host side, next to `image.env`) keeps `volume.tally`, one line:
`aiko_data <CreatedAt> <sentinel content>`. Before every `up`, the wrapper reads both
live values and compares:

| tally | volume | means | action |
|---|---|---|---|
| absent | no sentinel, no lock | first standup | proceed; the tally is written **after** the first healthy boot creates the sentinel |
| absent | lived | host predates the tally (both islands today) | adopt: write the tally from the live values and log that it did |
| matches | matches | the right volume | proceed |
| present | `CreatedAt` differs, sentinel matches | volume copied or restored into a new volume (a deliberate move) | refuse, and say how to re-adopt |
| present | sentinel differs or is absent | wrong volume, or a new empty one | **refuse** |

The re-adopt step is deliberately manual. A legitimate restore into a recreated volume
is exactly the case where a human should say "yes, this one".

**Why this has to live on the host, and what that costs.** The container can't see the
deploy dir, so the check can only run at doors the host owns:

- `deploy/compose.sh` (the only working compose door since #401: a bare `docker compose`
  refuses without the pin);
- `update.sh` and `standup.sh`, through it;
- design 18's watcher, which goes through the same wrapper.

A Docker daemon restart under `restart: always` skips the check. It also can't swap
the volume, so it skips nothing the check guards. That is the same door set and the
same limit as #401's pin, and #5186 would harden both the same way.

## Q7: the same host door, plus one rule

Images built before v0.17.1 ignore the sentinel. Rolling one of them back onto a
volume that has lost `aiko.db` gets the old behaviour: a fresh, empty island. Inside
the volume, the sentinel is a *floor*: it only protects you when you run an image at
or above it, the same weakness design 18 rejected for schema compat.

**The tally alone does not close this.** Q7's case is the *right* volume with its db
deleted: `CreatedAt` and the sentinel both match, so the tally passes. What closes it
is the migrator's own rule, *sentinel present and `aiko.db` absent ⇒ refuse*, run
**again in the wrapper**, where it doesn't depend on the image. The wrapper is in the
box's `deploy/`, and the rollback runbook already says to keep the current compose and
`deploy/` when rolling back below v0.17.2. So an old image is started through a new
wrapper, and the wrapper refuses before the old image ever sees the volume. It's one
check, in the same pre-`up` pass as the tally.

The cost is a second copy of a rule (see temper question 4). For the watcher, TEMPER
fold #9 already refuses an auto-restore to an image older than the guard. With the
wrapper rule, both doors are covered, human and automatic.

## Q2: the per-directory lock is a feature in prod

Noether r3: a directory that hosted db A refuses a fresh db B beside it while the lock
exists. In dev that is a nuisance. In prod it is door 2's second half: a `DB_URL`
changed to `/data/aiko2.db` finds the lock and refuses, where a per-db sentinel alone
would see no record for `aiko2.db` and build a new, empty island (with a warning
log). **Keep it.** For dev, the advice in the refusal text (delete the lock with the
stack down) already works.

## Q1: don't retire the lock as a witness yet; rename what it does

Its *bridge* role is finished: both live volumes carry a sentinel (measured above and
on #400). But the same `exists()` check also does Q2's job, and nothing else does that
job. Retiring it now saves one line in `_lived_evidence` and reopens door 2.

Proposal: keep the check, and fix the docstrings and the refusal text so they call the
lock what it now is, **the directory witness**, not "the bridge for volumes that
predate the sentinel". Revisit once the tally exists. If the tally also records the db
path, it covers door 2 from the host, and the lock can go back to being only a lock.
That is the coupling Tesla wanted dissolved, removed by giving its second job a better
home, not by deleting it.

## Q4 and Q5, as far as the measurements reached

- **Q5 (sidecars on restore).** Measured: no `-wal`, `-shm` or `-journal` on either
  volume. WAL is off (`db.py`), so the right worry isn't WAL. In the default `DELETE`
  journal mode the sidecar is `aiko.db-journal`, which exists only during a write
  transaction, or after a crash in one. A **hot journal** left beside a *restored*
  `aiko.db` is replayed into it on the next open, and it belongs to the old file. With
  the stack down for a restore, that needs a crash during a write, so it's rare, but
  the fix is one runbook line: *after restoring, remove any `aiko.db-journal` beside
  it*. Unmeasured: whether `update.sh`'s backup copy could ever carry one. The `.backup()`
  API produces a self-contained file, so I believe not.
- **Q4 (a half-restored file that still has `users`).** `PRAGMA quick_check` on a lived
  volume before `upgrade head` would catch most truncations. At 393 KB / 569 KB it
  should cost milliseconds. **Unmeasured.** Proposed as a separate, small PR with the
  timing measured on both volumes, not folded in here.
- **Q6 (refusal voice)** belongs to #393 and stays there.

## Questions for the temper

1. Is the door list for Q3 complete? It was enumerated from the compose file and one
   measurement. A missed door is the failure this note exists to prevent.
2. Does the tally's adopt row ("absent tally + lived volume ⇒ write it") re-open
   door 4 on the first run after this ships? It trusts whatever volume is mounted at
   that moment. Both islands are known-good today, so a one-time adoption is safe
   *now*. Is "adopt only with an explicit flag" the better default?
3. Is `CreatedAt` stable across a Docker upgrade or a daemon restart? It's believed to
   be, but that has only been seen across normal operation.
4. Is putting the "sentinel present, db absent" refusal in the wrapper, so it doesn't
   depend on the image, worth a second copy of a rule the migrator already has? Two
   copies of one rule is the drift pattern `schema_status` was built to end.

## Not in scope

The watcher itself (design 18 step b), #5186's literal override, and any change to the
migrator's sentinel. This note decides *what witnesses what*; the builds that follow
are design 18 (b) and a small `deploy/` PR for the tally.
