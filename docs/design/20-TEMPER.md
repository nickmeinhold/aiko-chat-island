# 20-TEMPER.md — design 20, what a volume can witness

**Overall verdict: RECAST** (4/4, independently). Round 1 of ≤3.
**Struck:** dt-1792832848, 2026-10-24. Families seated: Maxwell (Claude) + Noether (DeepSeek V4.1 Flash, high), Carnot (GPT-6.1, medium), Tesla (Grok, medium). Wu disabled. Full 4-way panel, no dark seat.

## Per-family verdicts
| Family | Verdict | One-line |
|--------|---------|----------|
| Maxwell (Claude) | RECAST | The sentinel identifies lineage, not generation; `CreatedAt` has no power to decide anything; adopt must be a ceremony |
| Carnot (GPT) | RECAST | The wrapper rule is already a different predicate. Remove Q7's machinery with an image floor; replace the door list with a state × transition matrix |
| Noether (DeepSeek) | RECAST | The adopt row is a circular witness; the wrapper rule doesn't run on every start; the door list is incomplete |
| Tesla (Grok) | RECAST | Missing tally must refuse like a missing `image.env`; db presence is a START-time property and the wrapper only sees CREATE time |

## Fatal flaws (deduped, most-severe first)

1. **Temper Q4 is settled, against the note: the wrapper's second copy is already narrower than the original, and it doesn't run on every start.** Raised by Carnot, Noether, Tesla.
   - It checks "sentinel AND file missing". The migrator checks "(sentinel OR lock) AND no `users` table". So a 0-byte file, a bookkeeping-only db or a lock-only volume passes the wrapper, and an image older than the guard builds an empty island. That is PR#116's drift, present in the design before any code.
   - It also runs only on `up`. Tesla's distinction: the image pin is fixed when a container is *created*, but whether the db exists is a fact at every *start*. `restart: always`, a daemon restart or `docker start` re-runs an old container after its db has been deleted, with no wrapper in the path. The note's claim that the restart path "skips nothing the check guards" is false.
   - Maxwell's argument that two fail-closed checks compose as a union of refusals is true, but it's about safety. This flaw is about coverage.
   - **DISPOSITION: remove the second copy.** Choice between Carnot's **image floor** (deploy policy refuses any rollback target older than v0.17.1; recovery below it becomes an explicit offline procedure) and Tesla's / Noether's **entrypoint shim**: a host script mounted as the gateway entrypoint in the kept compose file, so it runs on every start. **Recommended: the floor.** Every image at or above it runs the migrator's single predicate on every start, which already covers the restart path. It is the same rule TEMPER-18 fold #9 sets for the watcher, so one rule covers both doors. And it adds no second predicate in a second language. **Open for Nick:** is a rollback below v0.17.1 ever needed in production? If yes, the shim is the alternative, with a fixture corpus binding it to `_lived_evidence`.
2. **Adoption is automatic when it must be deliberate.** Raised by all four.
   - "Absent tally + lived volume ⇒ write it" creates the outside witness from the very volume it is supposed to judge (Noether: a circular witness).
   - It is the opposite of how the pin behaves: a missing `image.env` refuses, but a missing `volume.tally` would silently write itself (Tesla).
   - The first-standup row has a crash window between `up` returning and the sentinel existing (Tesla, Carnot).
   - **DISPOSITION: fold.** A missing tally refuses, whatever is on the volume. Syd and melb are enrolled once, at the hand-sync, by an explicit `adopt-volume` that shows both live values and requires a flag. A first standup writes `pending` before `up` and completes the tally only once the sentinel is visible (Tesla's protocol). The tally is written with the same atomic discipline as the sentinel. Design 18's refresh of the deploy dir must preserve the tally or fail closed.
3. **The check isn't tied to the mount actually being used.** Raised by Carnot, Noether, Tesla.
   - Inspecting the hardcoded `aiko_data` proves nothing if this invocation mounts something else: `compose.sh run -e DB_URL=…`, `-v` or `-f` overlays, or design 18's rehearsal side directory.
   - The note also never says how the sentinel gets read. A helper container that ran the normal entrypoint would itself run `migrate.run()` (Noether).
   - **DISPOSITION: fold.**
     - Resolve the gateway's mount and `DB_URL` from `docker compose config` for this invocation.
     - Read the sentinel through a no-entrypoint, read-only helper, failing closed when it can't be read.
     - Give rehearsal its own tally, or put it explicitly out of scope; it must never read or write the production one.
     - Scope the refusal to serving starts, so `logs`, `ps` and `exec` stay open for the restore path.
4. **Door 4 is overclaimed.** Raised by Maxwell, Carnot. The sentinel identifies *lineage*. A backup taken after 2026-10-20 carries the same sentinel, so a stale copy of this island matches. That is also what every legitimate restore looks like. **DISPOSITION: fold.** Split door 4 into 4a (another island's lineage: caught) and 4b (a stale copy of this lineage: indistinguishable from a legitimate restore, deliberately not refused). Narrow the tally's claim to lineage.
5. **`CreatedAt` is an unmeasured clock used as a hard refusal.** Maxwell and Tesla say drop it or demote it; Carnot says keep it as a conservative signal. Its only distinct row is "copied into a new volume", which is a legitimate move. A format change in a Docker upgrade would fire it on both islands the same night, and that would train operators to reach for re-adopt (Tesla). **DISPOSITION: fold, partly.** Take `CreatedAt` out of the refusal path. At most, a mismatch warns and requires `adopt-volume`. Keep it only as a recorded value until it has been measured across a Docker upgrade on a throwaway volume.
6. **The door list is not exhaustive and mixes causes with states.** Raised by Carnot (as a matrix), Noether, Tesla. Missing from it:
   - a bare compose with an exported pin (#5186);
   - `run -e`, `-v` and `-f` overrides;
   - `exec` deletion or truncation followed by a restart;
   - rehearsal;
   - losing the tally;
   - a right volume whose db is damaged, with its witnesses intact.

   **DISPOSITION: fold.** Rewrite Q3 as a state × transition matrix, every cell marked protected, refused, or out of the failure model. Add the measured fact (Maxwell, measured during this strike on syd): `docker volume rm` refuses, even with `-f`, while any container, stopped ones included, still uses the volume. The control succeeded once the container was removed.
7. **Volumes with only the lock can't be represented.** Raised by Noether. The tally and the wrapper rule both need a sentinel. **DISPOSITION: named tradeoff.** No live volume is lock-only (both measured carrying the sentinel). Explicit enrollment refuses a lock-only volume, with an instruction to boot an image at or above the floor first. Owner: whoever builds the `deploy/` PR.

## What holds (agreed across families)
- The split itself. A record inside the volume proves a past. Only a record outside it can prove the host meant *this* volume. All four kept it.
- Q2 reframed as a production feature, and Q1 keeping the lock as the directory witness until something outside the volume records the db path (4/4).
- `external: true` refusing a missing volume, measured with a positive control, closes door 1 for ordinary Compose (Carnot: for the tested configuration).
- The in-image single predicate (`schema_status`, `_lived_evidence`, `mode=rw`) stays single inside the image. Q7 must not fork it across the image boundary (Tesla).
- The Q5 runbook line for a hot `aiko.db-journal`, and keeping `quick_check` out until it has been timed.

## Disposition
**RECAST → fold flaws 1–6 into design 20 v2, name 7 as a tradeoff, and re-strike (round 2 of ≤3).** One decision is Nick's before the fold: **image floor vs entrypoint shim** for Q7 (flaw 1).

---

# The strikes, in arrival order

Bundle: `20-what-a-volume-can-witness.md` + a strike-focus sheet (lead target: temper Q4) + `migrate.py`, `worker_guard.py`, compose excerpts, `island-compose.sh` header, `compose.sh` (56,731 B).

`migrate.py`, `worker_guard.py`, compose excerpts, `island-compose.sh` header, `compose.sh`
(56,731 B). Effort: Noether high, Carnot medium, Tesla medium.

## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The tally is the right shape. But the note oversells what the sentinel identifies (lineage, not generation), prices temper question 4 against the wrong precedent, and lets Docker's `CreatedAt` turn every legitimate restore into a refusal while adding no power to decide anything.

`Hans Gruber: "When you steal $600, you can just disappear. When you steal 600 million, they will find you."` A stale copy of your own island steals quietly, and this tally will never find it.

**Fatal flaws:**
- **Door 4 is overclaimed (unstated assumption).** §Q3 says the sentinel's content "identifies *this island's data* (door 4)", and lists "an old copy of this one" under door 4. But `_write_sentinel` writes once per *lineage*. Every backup, `.backup()` copy or volume clone made after 2026-10-20 carries the same `06:01:11.810364` string. So an OLD copy of this island matches the tally and serves its stale data. That isn't a gap a better tally fixes: a deliberate restore from `backups/` is *exactly* a stale copy of this island, by design. The tally can tell *which island*. It cannot tell *which generation*, and it must not try, or it blocks every legitimate restore. The note has to say so, not claim door 4 closed.
- **Temper Q4 is priced against the wrong precedent (wrong option-frame).** PR#116's drift was dangerous because one of the two checks had a **serve-anyway** branch: the permissive copy diverged. Two **fail-closed** checks in series compose as the *union* of their refusals. If they drift apart, the stricter one wins and the island refuses a case it could have served. That's an availability bug and an operator-confusion bug, never a data-loss bug. So the second copy is safe *as long as the wrapper copy can only refuse, never permit*. The note should state that invariant, and forbid any wrapper override that could bypass the image-side check, instead of treating this as `schema_status`'s problem. (It is still worth asking whether the wrapper can run the ONE predicate rather than re-implement it: `docker run --rm <a guard-bearing image> python -m aiko_gateway.migrate --check-volume` against the volume, before starting any older image. One predicate, run from the host. Its cost is a read-only `--check-volume` mode and keeping a guard-bearing image locally, which `ghcr-retention` already does for `current`.)
- **`CreatedAt` has no power to decide anything, and it costs something (wasted work).** Go through the tally table without it. A recreated empty volume (door 3) already fails on "sentinel absent". A foreign island's volume fails on "sentinel differs". The *only* row `CreatedAt` changes is "sentinel matches, `CreatedAt` differs", which is a legitimate copy or restore into a new volume. `CreatedAt` turns that into a refusal. Dropping it also deletes temper question 3 (is `CreatedAt` stable across Docker upgrades?) instead of needing an answer. If restore friction is *wanted*, say so as a deliberate choice with its own row, not as a side effect of an identity field.
- **The adopt row is automatic when it should be a ceremony (missing failure mode).** "Absent tally + lived volume ⇒ write it" is right exactly once per box, at the hand-sync that ships this feature. After that, an absent tally on a lived host means the deploy dir was lost or rebuilt, which is the host-migration case where a human must look. Make adoption an explicit one-time `compose.sh` subcommand run during the hand-sync. Make the row refuse from then on, with the instruction in the refusal text. That also answers the note's own temper question 2.
- **The door list never says door 3 requires a `down` (missed strengthening, MEASURED during this strike).** Docker refuses `volume rm` on a volume referenced by any container, stopped ones included, and `-f` does not override it (syd, 2026-10-24: `volume is in use`, exit 1 both ways; the control with the container removed succeeded). So recreating `aiko_data` requires removing the containers first, and the next start must go through `up`, which goes through `compose.sh`. So door 3 has no back door through `restart: always` or `docker start`. State it in the note, because the whole tally argument depends on "every start after a volume swap passes the wrapper".
- **Design 18's phase C moves the db out of `/data` mid-transaction (under-counted blast radius).** The watcher's cutover moves `aiko.db` + `-wal` + `-shm` as a set and restores them on failure. Between the move and the restore, the volume reads "sentinel present, db absent". If any step in that window starts a container through `compose.sh`, the new wrapper rule refuses the watcher's own rollback. The note scopes the watcher out, but the two share a door. Say how they compose: for example, the watcher holds a lock the wrapper honours, or the restore completes before any `up`.

**What holds:**
- The core idea: proof that a volume has lived is internal, and proof that it's the right one has to be external and paired. That's the right epistemics, and the measurement table backs it.
- Q2 reframed as a production feature (door 2's second half) is correct and well argued.
- Q1 "keep the check, rename its role, revisit once the tally covers door 2" removes the coupling by re-homing the second job, which is the right move.
- Q5: identifying a hot `aiko.db-journal` as the real sidecar, not WAL, is a genuine catch.
- `external: true` refusing a missing volume, measured with a positive control, closes door 1 on evidence rather than belief.

**If RECAST, what to fold back:**
- §Q3 door 4: split it into 4a (a foreign island's lineage: caught) and 4b (a stale copy of this lineage: indistinguishable from a legitimate restore, deliberately NOT refused). Remove the claim that the sentinel identifies "this island's data" generation-wise.
- §Q7/temper Q4: state the invariant "the wrapper may only refuse; no wrapper flag bypasses the image's check", and argue that union-of-refusals composition is why a second fail-closed copy is safe. Evaluate the "run the one predicate from a guard-bearing image" alternative explicitly.
- Tally: drop `CreatedAt`, so the tally is `aiko_data <sentinel content>`. Delete temper question 3.
- Adopt row: a one-time explicit `adopt` during the hand-sync. Absent tally + lived volume refuses from then on.
- Record the measured fact under Q3: `volume rm` (even `-f`) refuses while any container, stopped included, references the volume.
- Add a line on the interaction with design 18 phase C: who may call `up` while the db set is out of `/data`.


## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** Keep the external expectation record, but recast its claims and lifecycle. The proposed wrapper check neither reproduces the migrator's guarantee nor covers every start; automatic adoption erases the independent expectation; and matching timestamps cannot detect an old copy of this island. The wasted work is extending rollback support below the safety floor with an incomplete second guard. Dijkstra: "Simplicity is prerequisite for reliability." No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are.

**Fatal flaws:**
- Q4: the proposed second rule is already different from the migrator's rule. The migrator considers sentinel OR directory-lock evidence, rejects existing files without users, and uses non-creating connections after inspection. The wrapper proposes sentinel AND missing-file only. An existing zero-byte database passes it; deletion between inspection and an old image's first connection also passes. This is precisely the predicate drift the note fears, present before implementation.
- The four doors are neither exhaustive nor mutually exclusive: they mix causes of loss with resulting volume states. A separate missing state is the right volume with a replaced, truncated, or empty database and retained witnesses. A separate missing transition is starting an existing container without up: start, restart, automatic crash restart, or daemon restart. The claim that daemon restart cannot swap the volume is insufficient: the mounted directory's contents can change through restore, host storage replacement, or another process without changing the container's mount declaration. An old image then starts without the wrapper check.
- Automatic adoption defeats the external witness exactly when it is needed. Missing tally plus lived volume does not establish 'host predates tally'; it also describes a lost deploy directory, an incomplete host restore, or another island's restored volume. Missing tally plus no witnesses similarly conflates first standup with total data loss. Today's measurements do not authorize an unattended adoption at some later first execution.
- The timestamp pair cannot detect the promised 'old copy of this one' case. A backup taken after sentinel creation carries the same sentinel content. Restoring that backup into the existing Docker volume preserves CreatedAt too, so both halves match while data has moved backward. A whole-volume snapshot restored with its metadata can have the same limitation. These are lineage witnesses, not freshness witnesses; the tally-stick metaphor overstates their proof.
- The checked resource is not yet bound to the resource started. Inspecting aiko_data on one daemon and reading its sentinel is insufficient unless the effective Compose model mounts that exact volume at the effective database path on that same daemon. A literal DB_URL establishes only one input. The bundle does not establish coverage for Compose override files, alternate execution modes, or concurrent deployment/storage changes. A pre-up observation also supplies no exclusion against replacement before boot.
- The lifecycle is underspecified. First boot can become healthy before the tally is durably committed; a crash then returns to automatic adoption. Re-adoption has no stated quiescence, validation, or atomic-write contract. Design 18's rehearsal intentionally uses a copied database in a side directory: applying the production tally there either refuses legitimate rehearsal or encourages a bypass whose scope is undefined.

**What holds:**
- An expectation stored outside the data volume can detect accidental volume replacement while that expectation survives independently.
- The measured external-volume refusal closes Compose's ordinary automatic creation path for the tested configuration. It does not establish the entire deployment boundary.
- The existing migrator's non-creating connections provide a materially stronger guarantee than an existence check before launch.
- Keeping the directory witness preserves protection against a fresh database filename beside an existing island. Recording a path in a tally replaces that job only when the checked path is demonstrably the path actually used.
- A recreated volume requiring explicit acceptance is a defensible operational policy. CreatedAt is useful as a conservative recreation signal, subject to documenting its supported storage lifecycle.

**If RECAST, what to fold back:**
- First dissolve Q7's extra machinery by setting a minimum supported production image containing the existing sentinel guard. Reject older rollback targets through the deployment policy; disaster recovery below that floor becomes an explicit, offline procedure. This removes the proposed second database-loss predicate. State that the image floor itself needs a defined enforcement boundary.
- If pre-sentinel production images are mandatory, acknowledge that this requires a larger design: one maintained guard, executed independently of the selected application image, with full lived-evidence semantics and non-creating access enforced through startup. Merely invoking shared code before an old image does not eliminate the race. Do not claim a host preflight is equivalent to the migrator.
- Replace inferred bootstrap with explicit initialize and adopt operations. Normal operation refuses a missing tally, regardless of volume contents. Enroll syd and melb deliberately against independently confirmed expectations. Adoption validates the selected database while stopped and commits the tally atomically and durably; it never manufactures its own authorization from whatever happens to be mounted.
- Rewrite Q3 as a state-and-transition matrix: missing volume; empty replacement; foreign lineage; stale same-lineage copy; missing/empty/damaged database; changed path or mount; missing external expectation. Cross these with create/up, start/restart, automatic restart, restore, host migration, and rehearsal. Mark each cell protected, refused, or explicitly outside the failure model.
- Narrow the tally's guarantee to expected volume incarnation and data lineage. Freshness requires independent restore provenance or an external progress expectation with a defined update protocol. If that cost is unjustified, explicitly exclude stale same-lineage restores from automatic detection rather than selling timestamps as proof.
- Bind validation to the effective deployment target, serialize deployment and restore operations, and define rehearsal as a separate target that cannot enroll or overwrite production expectations. Specify recovery from a crash between initial database creation and tally commit. The second law applies here: adding witnesses creates state entropy unless every transition has an owner.


## NoetherNullPointer's Design Strike

**Verdict:** RECAST

**Summary:** The design claims an external tally conserves “this volume is the right one,” but the adopt row mints the tally from the very volume it must judge, and the Q7 wrapper rule is a weaker, differently-scoped predicate that does not run on every path that can present an empty db to an old image.

**Fatal flaws:**
- **Circular witness — 20 §“The proposal: a host-side tally.”** The adopt row `absent tally + lived volume => write the tally from live values` trusts whatever is mounted at first run, after a host rebuild, or after `volume.tally` is lost. That is door 4 (wrong lived volume) and door 3 (recreated/missed restore) in disguise. The external witness must be independent of the thing it witnesses; here it is generated by reading the volume, so it cannot prove the volume is the right one. “Both islands are known-good today” is a one-time assumption, not an invariant.
- **Q7 wrapper rule is not the migrator’s rule and does not run on every start — 20 §Q7; 10-migrate.py `_migrate`.** The migrator refuses when `evidence` exists (sentinel **or** worker lock) and `"users" not in tables`. The wrapper rule as stated checks only “sentinel present and `aiko.db` absent,” so it misses lock-only evidence, 0-byte db, bookkeeping-only db, and any db present without `users`. Worse, it runs only on `up`; `restart: always` and Docker daemon restarts re-run the container entrypoint without the wrapper, so an old image can still see a missing/empty db and birth an empty island. The claim “it skips nothing the check guards” is false for Q7.
- **Q3 door list is incomplete — 20 §Q3.** It omits bare `docker compose` with `ISLAND_IMAGE` exported (explicitly open #5186 in 13-island-compose-header.sh) plus `-f` overrides; `docker run` / `docker compose run` with a different volume or `DB_URL`; `docker exec` deletion/truncation followed by `restart: always`; design 18’s rehearsal side directory; and host rebuild/tally loss. A missed door is the failure this note exists to prevent.
- **Host-side sentinel read is underspecified and can itself be the door — 20 §“proposal.”** `docker volume inspect` gives `CreatedAt`, not file contents. Reading `.aiko.db.initialized` inside the volume requires root access to Docker’s data-root or a helper container mounting `aiko_data`. The design does not define a read-only, no-entrypoint, no-side-effect mechanism; a helper that runs the normal entrypoint would run `migrate.run()` and create the empty island. Fail-closed behavior when the sentinel is unreadable is unstated.
- **Lock-only lived volumes are unrepresentable — 20 §Q1; 11-worker_guard.py.** The tally is `aiko_data <CreatedAt> <sentinel content>`, and the Q7 wrapper rule requires sentinel. But `_lived_evidence` treats the worker lock as lived evidence. A volume with lock but no sentinel — the bridge case the lock exists for — cannot be adopted or guarded by the host rule. Today’s two islands have sentinels, but the design says the lock remains for Q2 and pre-sentinel volumes.
- **Tally does not record db path, so door 2 is only half-closed — 20 §Q2.** The proposed tally is volume identity only. A compose override changing `DB_URL` to another file in `/data` passes the host tally; the per-directory lock catches it only inside images that enforce it. For old images (Q7), the wrapper does not check db path, so the door remains open via bare compose/override.

**What holds:**
- The measurement that `external: true` prevents Compose from creating an empty volume is sound.
- The sentinel inside the volume is a valid floor for images at/above v0.17.1; the migrator’s fail-closed checks and `schema_status` single-predicate discipline are correct.
- Keeping the worker lock as the directory witness for Q2 is sound. The idea of an external witness paired with an internal one is the right shape; its bootstrap and execution points are broken.

**If RECAST, what to fold back:**
- Replace auto-adopt with explicit, fail-closed adoption. Absent tally + lived volume ⇒ refuse. Require an operator command that asserts expected `CreatedAt`/sentinel, or seed `volume.tally` manually from known-good inventory. Do not mint the witness from the live volume.
- Move the Q7 guard from `deploy/compose.sh` to a host-owned entrypoint shim or init container that runs on every container start, including `restart: always`. Make it the single host predicate: evidence (sentinel or lock) and `users` table present, db path matches. If old images cannot be wrapped, state that Q7 is not closed for them and forbid them on lived volumes.
- Expand and classify the Q3 door list: bare compose with exported pin, compose `-f` overrides, `docker run`/`run`, `docker exec` + restart, design 18 rehearsal side directory, host rebuild/tally loss. For each, either guard it or declare it out of scope.
- Define the wrapper’s sentinel-read mechanism: `docker run --rm --read-only --entrypoint /bin/sh <pinned-image> -c 'cat /data/.aiko.db.initialized'` or a host bind mount, with fail-closed on unreadable, and guarantee it cannot run the app entrypoint.
- Extend the tally format to include the db path and lock evidence, or explicitly state it does not cover door 2 and lock-only volumes.

## Tesla, the Arc-Prophet's Design Strike

**Verdict:** RECAST

**Summary:** The tally stick is the right instrument, but absence of the outside half currently canonizes whatever volume is mounted, and the second copy of the sentinel rule is tuned an octave off the failure that will ring: a present, empty database started again by the daemon long after `compose.sh` has left the room.

Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."

**Fatal flaws:**
- **The adopt row is an empty pin, and design 18 is the finger that will pluck it** (proposal table, "absent tally + lived volume"; temper Q2). `image.env` going missing refuses. `volume.tally` going missing writes itself from the live mount and calls that adoption. Same species of box-local file, opposite polarity to the pin you just finished fail-closing. Deleting the tally, rsyncing a deploy dir that never tracked it, or a watcher that refreshes `deploy/` and drops the untracked half, re-blesses door 4 forever. Both islands being known-good today is a measurement of this afternoon, not a property of the rule. The crash window falls into the same row: `up -d` returns before the entrypoint's sentinel exists, so "write the tally after the first healthy boot" has no happens-before edge, the boot dies in between, and the next pass sees lived-and-untallied and adopts.
- **Temper Q4: the second copy is the wrong frequency, and it is already a narrower sentence than the original** (Q7; `run()` / `_lived_evidence` / the "no users table" refusal in `10-migrate.py.md`). The migrator refuses when lived evidence is present and the island is absent. Lived evidence is the sentinel or the worker lock. The island is absent when the file is missing or when it has no `users` table, including the 0-byte file a half-connect leaves behind. The wrapper sentence is only "sentinel present AND `aiko.db` absent". A botched restore leaves a file that exists. The tally matches, the wrapper proceeds, the pre-0.17.1 image builds a fresh empty island, and `restart: always` seals it. That is PR#116's drift, shipped in the design of the cure. Placing that sentence in the pre-`up` pass and then dismissing daemon restart by analogy with #401 couples two invariants that do not share a lifetime. The pin is create-time: restart does not re-resolve the image. Database presence is start-time. A stopped container still references the volume object, so restart cannot `volume rm` it; restart can and will start an old image whose bytes were deleted after the container was created. The note hears "no swap" and stops listening. The hum is under the swap.
- **Q3's four doors were read off the compose file, and the door you are about to trust is the wrapper's open mouth** (`14-compose.sh.md` forwards `"$@"`; Q3 doors 1–4; "Not in scope" still sends design 18's watcher and rehearsal through that same wrapper). Door 2 is a literal `DB_URL` only until `deploy/compose.sh run -e DB_URL=sqlite+aiosqlite:////tmp/empty.db`. A `-v` or `-f` overlay can put a different tree on `/data` while `docker volume inspect aiko_data` still sings the tally's song, so you bless the volume you did not mount. Design 18's rehearsal is that overlay on purpose: a side directory, a `.backup` copy, no client port, same wrapper. One tally keyed to the hardcoded name `aiko_data` will either refuse the rehearsal for the production volume's sins, or write the production tally from the side copy's `CreatedAt` and sentinel. A witness that does not resolve *this invocation's* mount for the gateway is a witness for a volume that may be standing beside the blast, nodding.
- **`CreatedAt` as a string is an unmeasured clock wired across both islands as a hard refuse** (temper Q3; the table's "`CreatedAt` differs, sentinel matches" row). You already say stability across a Docker upgrade has only been believed, then you refuse the island on inequality. A format rewrite of the same instant (`2026-07-05T00:17:38Z` against the same second with a fractional tail) looks identical to door 3 on syd and melb the same night. The printed remedy is re-adopt. A breaker that fires on routine daemon maintenance teaches re-adopt as the gesture that stops the noise, and door 4 walks through a gesture the operator has practiced. The empty-volume case does not need this clock: sentinel absent already refuses. `CreatedAt` is only the witness for "these bytes were copied into a new volume object." That is a real door. It is not a door to close with a string compare you have not struck with a live upgrade.

**What holds:**
- The split itself. A record inside the volume can prove a past. Only a record outside it can prove this host meant *this* volume. Pairing them is the witness the container was never going to grow. Doors 3 and 4, as volume-object problems, are the right shape, and the measured `external: true` refusal really does shut door 1.
- Keeping the lock as the directory witness. Retiring it reopens door 2 for a second file inside `/data`. The rename is honest. The coupling can leave once something outside the volume records the db path.
- A deliberate copy into a new volume object staying a human "yes", with no serve-anyway. That is the one re-adopt that should exist.
- The in-image predicate staying single *inside* the image (`schema_status`, `_lived_evidence`, `mode=rw`). Q7 does not get to fork that function across the image boundary. The boundary is the reason an old image is dangerous.
- Q5's runbook line for a hot `aiko.db-journal`, and leaving `quick_check` unmeasured and unfoldered until it has been timed on both volumes.

**If RECAST, what to fold back:**
- **Replace the adopt row.** Absent tally and no lived evidence is the only row that may create a binding: before `up`, write `pending <CreatedAt>` (atomic rename, same discipline as `_write_sentinel`). Complete it to `aiko_data <CreatedAt> <sentinel>` only after the sentinel is visible and `CreatedAt` is unchanged. Pending plus the same `CreatedAt` may retry. Absent tally and any lived evidence refuses, and prints a separate `adopt-volume` command that shows both live values and waits for an explicit flag. Losing the tally must refuse, the way losing `image.env` refuses. State that design 18's deploy-dir refresh has to preserve this file or fail closed.
- **Do not copy the migrator sentence into the wrapper.** For Q7, add one host script, `deploy/volume-guard`, and mount it as the gateway entrypoint in the compose file you already keep across rollback, so the guard is in the container spec and runs on daemon restart, not only on `compose.sh`. It execs the image's real entrypoint only if it passes. Its predicate, written once: lived evidence (sentinel or `.aiko-worker.lock`) and island absent (no `aiko.db`, or size 0). Point a fixture corpus at that script and at `refuse_missing_db_message`'s conditions so the two languages cannot diverge in silence. Scope it to the serving start. `logs`, `ps`, and `exec` stay open, or the refusal will brick the restore path it tells the operator to use. `run --entrypoint` remains the deliberate bypass, in the same class as an exported pin.
- **Bind the tally to the mount this invocation will actually use**, taken from `docker compose config` for the gateway service, not from a hardcoded `aiko_data` inspected beside the real `-v`/`-f`. Rehearsal gets its own tally keyed by its own volume, in the side directory, and must not read or write the production line.
- **Treat `CreatedAt` as an instant, and do not hard-refuse on it until it has been measured across a Docker upgrade on a throwaway volume.** Until that measurement, a `CreatedAt` mismatch warns and requires `adopt-volume`. Sentinel mismatch or sentinel absence with a tally present still refuses. Record in the design that string inequality is not yet a witness.
