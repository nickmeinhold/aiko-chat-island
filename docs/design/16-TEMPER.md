# TEMPER.md — declarative deploy v2 (design 16 / #2301)

**Overall verdict: RECAST — unanimous, 4/4 families. No DISSOLVE.**
**Struck:** `dt-1790268238`, 2026-09-24. Maxwell (Claude), Kelvin (Gemini 2.5-pro), Carnot
(Codex/GPT), Tesla (Grok). Wu/Kimi disabled. **Full panel, no dark seat.**

The direction survives. The assembly does not. Every family affirmed that **push delivery
genuinely dissolves design 15's bootstrap loop** — the cargo/vehicle cut is real and is why
this ore is not slag — and every family found the same class of overclaim underneath it:
**the design says "atomic," "dissolved," and "closed" about things that are none of the
three.**

Tesla's one-line diagnosis: *"The fetcher that could not fetch itself is dead, but three
clocks — the files, the image, the schema — are still ringing, and this design buries them
under one symlink and calls the silence a dissolution."*

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Maxwell (Claude) | RECAST | `GENERATION.txt` is a predicate/lifecycle bug in the section that brags about it; generations leave box-local state with nowhere to live. |
| Kelvin (Gemini) | RECAST | *"The loop is not dissolved; it has simply moved into the meat."* And the atomic flip polishes the handle on a blade. |
| Carnot (GPT) | RECAST | §5 collapses drift detection and change delivery — a legitimate change is indistinguishable from unauthorised drift. |
| Tesla (Grok) | RECAST | §2 and §5 are the same wire pulled to opposite grounds — the shape that killed design 15's increment 2. |

---

## Fatal flaws (deduped, most-severe first)

### 1. The backup — the only undo for the recorded FATAL — is stored inside the thing that rotates. **[Tesla]**

`update.sh` is specified as unchanged, and is now invoked from `current/deploy` — that is,
`releases/<ts>/deploy`. **An unchanged `update.sh` resolves its relative backup path into the
generation.** §6 then caps retention and deletes old generations.

So the sqlite hot copy — the only thing standing between a failed migration and data loss —
lands in a directory the design deliberately prunes. *"A generation that feels like a closed
cohort and prunes like a log is how the spine becomes a souvenir."*

**DISPOSITION — fold, and gate the build on a test:** with cwd at `releases/<ts>/deploy`, the
backup must resolve to `<REMOTE_PATH>/backups`, asserted in CI. Prune must never unlink the
target of `current`, never unlink the generation of the running digest, and must shred `.env`
before removing a generation.

### 2. `deploy/**` ships every island's ciphertext to every island. **[Tesla]**

§3 says "**ALL** of `deploy/`." `deploy/secrets/` contains `enspyr.env.sops` *and*
`imagineering.env.sops`. The quantifier had no denylist, so **enspyr receives imagineering's
encrypted config and vice versa.** It also over-includes control-side-only scripts
(`verify-secrets.sh`, the shipper itself) that §3a of design 15 established have no business
on a box.

**DISPOSITION — fold:** replace the quantifier with a **closed manifest, tested in CI**.
Shipped set = `docker-compose.yml`, `mosquitto.conf`, `deploy/**` **minus** a denylist naming
`deploy/secrets/**`, `deploy/islands/**`, and every control-side-only script. *"The denylist
is the closed set; 'legitimately local' is not a membership rule."* Say explicitly whether
`caddy/` is in or out — superseded v1 carried it as repo-authoritative and design 16 silently
drops it.

### 3. §2's central claim is false in the way §8 already admits, and §2/§5 are the same wire pulled to opposite grounds. **[Tesla, Kelvin, Carnot, Maxwell — all four]**

The narrow claim survives: box-resident code no longer has to fetch its own replacement.
Everything §2 builds on top of it does not.

- **§5 will not place a generation until the box answers** (`current` readable, island
  identified), and COULD NOT RUN fails closed — *"a dangling `current` is a repair that
  requires the artifact whose corruption is the failure."*
- **§6 will not place one without the age key.** Because `.env` is inside the cohort, **a
  one-line public compose forward — the exact 2026-09-11 repair — now cannot move unless
  that key is alive.** Key loss blocks every config change, not merely secret rotation.
- **Break-glass is hand-edit, which is the drift the next ship REFUSES.** Recovery is
  outside the mechanism by construction.
- Kelvin from the other side: the unsynced `deploy/` on the box is replaced by **an unsynced
  control-side checkout on the operator's laptop.** A stale clone produces a malformed
  generation. Entropy conserved, not destroyed.
- §8 already says the limit is closed only for operators who walk this path and *"should not
  be described as closing it"* — contradicting §2 in plain words.

**DISPOSITION — rewrite §2, delete §1's thesis line.** The true claim, in words that cannot
be quoted as "the loop is gone": *the box-resident fetcher loop is dead; the control side is
a single point of availability and of compromise for every island it ships; ISL-0003's limit
is unchanged for anyone not using this path.* Add a **second age recipient** so one laptop is
not the only decrypt path. Specify break-glass as *placing a generation and swinging
`current`* — never editing live files, or the next ship refuses the repair.

**NOTE on Kelvin's proposed fix — REJECTED.** Kelvin proposes making the shipper a
`curl … | bash` remote fetch. That is remote code execution on the operator's machine, which
is ISL-0003's founding objection to push-CD. **The finding is right; the fix is not.** Pin the
shipper's provenance some other way.

### 4. §4 and §7 make the FATAL *more likely*, then leave the trigger as an open question. **[Tesla, Kelvin, Maxwell]**

*"Before it, nothing is live; after it, everything is"* is v1's overclaim relocated from `mv`
into the word **live**. `rename(2)` swings one directory entry. Containers, the pulled image
and `aiko_data` do not swing with it.

- A crash between flip and recreate makes `GENERATION.txt` lie about what is running — **the
  exact question ISL-0003 says a pin exists to answer.** Maxwell: a file describing what was
  *placed* is a predicate; what the island runs is a lifecycle fact, and §3 grants the
  predicate ISL-0003's authority.
- **`ISLAND_VERSION` lives in the cohort's `.env`**, so a compose fix and a migration become
  one gesture whenever the encrypted env has been bumped.
- `restore <ts>` is what an operator reaches for when `/health` goes red, and §7 admits that
  failure is often a migration. **Flip-only:** disk says old pin, process is new schema.
  **Flip-and-rerun:** `update.sh` pulls the old image onto a forward-migrated store. *Either
  reading plays the FATAL.*
- Kelvin: *"A design that makes a dangerous operation feel safe is a trap… it kills the
  healthy fear that must accompany a stateful migration."*
- Forbidding a scheduler *inside the generation* does not forbid one on the control side —
  now the natural home for a Friday cron — and inherited `--yes` is the accelerator.
- "It must say so where an operator reads it" is **prose functioning as executable
  governance**: the class this design quotes as its warrant.

**DISPOSITION — split the one command into two, and close §9.3 inside the design:**
- `ship-config` — refuses if `ISLAND_VERSION` or the image digest differs from what is
  running; does not pull.
- `ship-release` — the only path that may change the pin; **pulls by the digest the control
  side resolved, not by tag**; no bare `--yes`.
- `restore <ts>` — legal only when the generation left and the generation entered name the
  **same digest**; across a digest boundary it refuses and prints the absolute path of the
  sqlite backup. **No image rollback in the tool.**
- Replace the atomicity sentence with: *the swing atomically changes which files `current`
  names; running containers change only after `up` returns healthy.* Two facts on disk —
  **intended** and **running-as-of-last-green** — and incident response must be told which it
  is reading.
- **Pin the syscall: `mv -T`.** A symlink-to-directory destination **nests the new generation
  inside the old one** and the "one inode" swap never happens.
- `GENERATION.txt` records a digest while inherited `update.sh` pulls a mutable tag. Design
  15's temper already held this cheap gain: **bind the digest.**

### 5. §5 collapses drift detection and change delivery — the predicate does not name the state it proves. **[Carnot]**

If the control side diffs the box's current generation against the generation about to ship
and refuses on difference, **a legitimate config update is indistinguishable from
unauthorised local drift.** A specification bug, not an implementation detail. And §1's
thesis ("stop detecting drift") contradicts §5 (which keeps a drift gate) — *"a detector in
front of a mechanism that was supposed to delete the need for the detector."*

**DISPOSITION — fold, as Carnot's recast name: baseline-addressed control-side generations.**
Three states, never two:
- **baseline** — the generation the operator believes the box is running,
- **target** — the generation being shipped,
- **live** — what ssh actually reads from `current`.

**Refuse only when `live != baseline`** (that is drift). Show `target - baseline` as the
intentional change for human review, and **do not call it drift.**

### 6. §5a trusts a letter the shipper wrote to itself, on a daemon whose name is not the path. **[Tesla]**

After adoption, island identity is `current/GENERATION.txt` — **authored by the control
side.** A wrong first write poisons every later preflight: the check confirms the box matches
the lie. v1's flaw 6 was that confining writes to `REMOTE_PATH` does not confine *effects*,
and this preflight still only reads files at the path.

Docker's identity is project `aiko` plus external volume `aiko_data`, **constant across
directories on a shared host**. An empty wrong path has no `current` and no `.env`, which §8
treats as *adoption* → write a generation, run `update.sh`, and `compose up` **attaches to the
real tenant's volume from a decoy directory.** Split brain, with the drift detector pointed at
the decoy.

**DISPOSITION — fold:** **absence of `current` must not be genesis.** Genesis is `standup.sh`,
once. Deletion, a dangling symlink, and "please adopt" are three different states; the design
hears them as one. Adoption is an explicit `--adopt` flag. On **every** ship, ask the daemon —
read live `.env` `DOMAIN` and ask docker which compose-file path project `aiko` was last
started from; refuse unless that path resolves under this `REMOTE_PATH`.

### 7. §5 sizes the human compiler for one line and then hands it 2,373. **[Tesla]**

ISL-0003's refuse-and-show worked because the 2026-09-11 diff was **one forwarding line a
person could read**. Once the cohort is all of `deploy/` plus compose plus `.env`, the safety
property saturates: *"the operator rubber-stamps a wall, and the line that matters is the one
they did not see."*

Worse, the classes are not equivalent. **Hunks under `deploy/*.sh` are not config** — on
enspyr they execute as `sudo -n docker`, daemon-root, the moment the control side invokes the
new `update.sh`. One yes-no covering both is how a bad script hides inside a legitimate
compose change.

**DISPOSITION — fold:** present the pre-ship diff as per-file hunks, and require a
**separate acknowledgement for executable hunks under `deploy/**` from the acknowledgement for
config hunks.** *"The human is the compiler only if the two privilege classes are not one
yes."*

### 8. Generations leave legitimate box-local state with nowhere to live. **[Maxwell]**

§5 quotes v1's disposition approvingly — *"a box may legitimately carry local state, and
silently clobbering it is how #2301 became standing instead of caught"* — and then removes
the state's home. An operator's 3am edit to `current/.env` is not clobbered; **it is
orphaned**, which is worse, because the file still exists and still says what they wrote.

**DISPOSITION — fold:** either a `local/` directory outside `releases/` that the generation
composes over, or an explicit REFUSED when `current/` has been modified since placement (a
manifest hash) — turning an orphaned edit into a caught one.

### 9. §8 drops the cutover gate v1's temper had already folded in. **[Tesla]**

*"Writes generation 1 around"* the live `.env` either consecrates box drift as the new
authority or clobbers production secrets with whatever is in the sops file. Temper finding 1
killed byte-match-**as-proof**; it did not kill the **direct diff of the real object**, and
that diff is simply missing.

**DISPOSITION — fold:** before the first flip, `diff` the decrypted sops bytes against the
live `.env` and **REFUSE on mismatch**. Do not consecrate and do not clobber in one gesture.

### 10. Hygiene the design omits. **[Tesla]**

Generation directories `0700`, `.env` `0600`, **created mode-correct rather than
chmod-after**. Fail if `releases/<ts>/` already exists. **Shred the laptop's temp plaintext on
every exit** — §6 specifies the box and forgets the pocket.

---

## What holds (unanimous — do not recast these away)

- **The cargo/vehicle cut is real and is why this is not slag.** Design 15 died because the
  fetcher had to fetch itself; a push from a checkout does not. Tesla: *"Do not recast this
  back into a shim, a contract integer, or an image entrypoint. That corpse stays buried."*
- **The subtractive middle holds.** No `.env.template`, no `envsubst`, no render, no
  missing-var map, no byte-match gate. **A templating layer in the recast is the fossil
  returning and should be struck on sight.**
- **One same-filesystem `rename(2)` of a single symlink is the right joint — for FILES.**
  Staging under `REMOTE_PATH`, never `/tmp`. Generation-addressed restore. Only the claim
  about what the swing makes *live* is rotten.
- **Secret posture:** age key control-side, SOPS off both boxes, decrypt to `umask 077` file
  never `export`, `<island>.conf` parsed as data never `source`, capped retention.
- **Three outcomes, fail closed, never warn-and-continue.** `aiko_data` stays an external
  volume the cohort must not reach. `standup.sh` stays box-resident orchestration and is not
  an argument for policy in the image.
- **Option 3's preflight is a real compiler and must not be re-solved.** Tesla, on what still
  earns the build after the fold: *"the half it cannot see: `.env`, plus `deploy/` arriving as
  cargo instead of as a manual prelude. Narrower than 'the class is closed.' Worth doing. Not
  worth doing as one gesture that also migrates the database."*

---

## Disposition

**RECAST — round 1 of ≤3.** Fold flaws 1–10 into `docs/design/16-declarative-deploy-v2.md`
and re-strike. No family voted DISSOLVE; the direction is Nick's pick and survives.

**Two flaws are data-loss-grade and gate any build regardless of how the rest folds:**
flaw 1 (the backup lands in the pruned generation) and flaw 2 (every island receives every
island's ciphertext). Neither is a design-taste question.

**Sequencing note:** flaw 4's split (`ship-config` / `ship-release` / digest-bound `restore`)
is the largest structural change and touches flaws 1, 3 and 5 as well. Fold it first; several
other dispositions get simpler once the pin can only move on one command.

**Still true from v1's closing rule:** this assembly must survive a clean ≥2-family strike
*after* the fold, and the built PR still owes a `/cage-match` — this is deploy and secret
handling, which is cage-match-by-law.

---

# ROUND 2 — struck 2026-09-25, `dt-1790306164`

**Overall verdict: RECAST (3) vs DISSOLVE (1) — RECAST stands, but on notice.**
Full panel, no dark seat. Maxwell RECAST, Kelvin RECAST, Tesla RECAST, **Carnot DISSOLVE**.
DISSOLVE is decisive at ≥2 families; one is a strong finding to answer, not a kill.

**The round's real result is not the verdict.** Two families independently concluded that the
*next* fold must be **smaller** or the answer is option 3:

> **Carnot:** *"The fold repaired the design by spending the justification… The design's own
> mitigations are evidence against it."*
>
> **Tesla:** *"§9.6 is already groaning: this paper is past 'a small control-side shipper plus
> restore.' The next fold gets smaller. If these six corrections cannot be specified without
> new machinery, round 3 is option 3."*

Round 1 found ten flaws of design. Round 2 found six of **mechanism** — smaller, more
concrete, and three of them are things that simply do not work as written. That is the right
trajectory for a recast. It is also the trajectory that runs out of road.

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Maxwell | RECAST | Break-glass requires the key whose loss is the emergency; `baseline` has no specified home. |
| Kelvin | RECAST | `local/` resurrects two-sources-of-truth; "Mitigations" should read "Accepted Risks" — compromise is untouched. |
| Carnot | **DISSOLVE** | Fails its own economy test. The engine is now larger than the work it extracts. |
| Tesla | RECAST | `mv -T` as written errors; the backup gate a constant can satisfy; a stopped project reads as a missing one. |

## Did the fold discharge round 1?

| R1 flaw | Verdict | Note |
|---|---|---|
| 1 — backup in pruned generation | **PARTIAL** | the gate cannot fail the way the bytes fail (see R2-1) |
| 2 — every island's ciphertext | **PARTIAL** | today's paths denied; `deploy/**` still defaults the *next* path to cargo |
| 3 — §2's false dissolution | **DISCHARGED** | thesis dead, claim honest; compromise named but untouched |
| 4 — FATAL armed / atomicity / digest | **PARTIAL** | split + digest are right; `mv -T` operands missing; `ship-config` has no legal sibling |
| 5 — drift/delivery collapse | **PARTIAL** | three states named; the equality compares a belief to a directory |
| 6 — self-letter identity | **PARTIAL** | absence no longer genesis; the daemon question goes silent when stopped |
| 7 — human compiler | **DISCHARGED** | two privilege classes, two acknowledgements. *Do not grow a third prompt and call it a gate.* |
| 8 — local state homeless | **PARTIAL** | `local/` is now the bypass the refusal teaches |
| 9 — cutover gate | **DISCHARGED** | |
| 10 — hygiene | **DISCHARGED** | |

## New flaws (most-severe first)

### R2-1. `mv -T`, as §4c words it, does not succeed — and its natural repair is the bug it was added to kill. **[Tesla, checked before striking]**

§4c pins the flag and omits the operands. An implementer writing from *"swung with `mv -T`"*
produces `mv -T releases/<ts> current`: **source is a directory, destination is a symlink,
which is not a directory — GNU `mv` refuses** (`cannot overwrite non-directory with
directory`). The 3am repair of that error is **dropping `-T`**, and a plain `mv` of a directory
onto a symlink-to-directory **nests the new generation inside the old one** — precisely the
defect `-T` was added to prevent.

Every later invariant — prune the target of `current`, the backup arithmetic, "under
`releases/`" — assumes a shape the literal syscall never produces.

**FOLD:** write both commands — `ln -s releases/<ts> current.next` then
`mv -T current.next current`, symlink onto symlink, one `rename(2)`, both under `REMOTE_PATH`.
State that `mv -T` of the *generation directory* onto `current` is a **specified failure**, not
an implementation choice. **Fail closed if `mv` has no `-T`; never fall back to plain `mv`.**

### R2-2. The backup gate runs where the failure cannot occur, and a constant satisfies it. **[Tesla, Maxwell]**

Round 1's worst flaw was discharged *conditionally on a CI test*, and the test as specified
asserts **a resolved string at a synthetic cwd**. `printf '%s/backups' "$REMOTE_PATH"` passes
it — without reading that cwd, without executing `update.sh`, without an inode.

Worse, Tesla traced the real path: the backup is written by inherited `update.sh` via Python
`.backup()` **inside the container**, onto a bind whose host side **Compose resolves from the
canonical project directory**. `current` is a symlink; the daemon's project directory is
`releases/<ts>`. The same relative arithmetic that is correct from *logical* `current/deploy`
is **one directory short from the physical cwd** — and lands in the generation retention then
prunes. *"At 3am the green check and the shredded sqlite are the same generation."*

**FOLD:** the gate invokes the backup logic that actually ships, from **both** `current/deploy`
and the physical `releases/<ts>/deploy`, and asserts **the canonical parent of the written
inode** is `<REMOTE_PATH>/backups`. A string helper must not be able to pass it. Relative binds
in the shipped compose file are part of the same assertion.

### R2-3. A stopped project and a missing project are the same signal, and the signal is `--adopt`. **[Tesla, Maxwell]**

§5c asks the daemon *which compose-file path project `aiko` was last started from.* That is not
a project record — it is **two container labels**, and `docker compose ls` / `docker ps` list
**running** projects by default. `docker compose down` removes the containers and therefore the
labels — **and does not remove an external volume.** `aiko_data` survives, and `aiko_data` *is*
the tenant.

§5c reads the empty answer as *"the project does not exist"*, and that branch plus `--adopt` is
allowed to `compose up`. **Adopt of a decoy `REMOTE_PATH` onto a surviving `aiko_data` is the
split brain flaw 6 existed to close, entered through maintenance instead of through a missing
`current`.** An inspect *error* is also not classified as COULD NOT RUN, so daemon failure
types as the same empty. *"The design asks the fact the daemon forgets and ignores the fact it
keeps."*

**FOLD:** three daemon answers, not two. Labels present and the path under this `REMOTE_PATH` →
proceed. Labels present, path elsewhere → REFUSE. No containers → **do not call this absence**:
if external volume `aiko_data` exists, this is a **stopped tenant** and `--adopt` is REFUSED.
Inspect errors are COULD NOT RUN. Query `ps -a` / the labels, never default `compose ls`.

### R2-4. The split re-couples through the one file, and the half-swing is typed as drift. **[Tesla, Maxwell]**

`ISLAND_VERSION` lives in the cohort `.env`. `ship-config` refuses when the pin differs from
what is running and never pulls; `ship-release` is the only other mover and it pulls. So the
ordinary repair — a compose forward, the 2026-09-11 line — is legal **only while the encrypted
env's pin already matches the running image.** The moment git is ahead (**the normal state
between bump and release**), or the box has drifted, or the island tracks a moving tag, **the
safe door is shut and the dangerous door is the only door.** *"Two labels on one wire."*

And §5 makes the crash window worse: **baseline** is *what the operator believes is running*;
**live** is *what ssh reads from `current`*. §4 correctly says those diverge when the swing
landed and `up` did not — so `live != baseline` **REFUSES, and the repair of a half-applied
ship is classified as drift.** Maxwell, independently: `baseline` has **no specified home** —
control-side it is a fourth thing that can drift; box-side it *is* `live` and the distinction
collapses. *"Three states were named; the equality compares a process-belief to a directory."*

**FOLD:** take `ISLAND_VERSION` out of the blob `ship-config` replaces, **or** have
`ship-config` rewrite the placed pin to the **running** digest, show that override, and refuse
to `up` any other image. Name the half-swing as its own state with continue-or-restore, not as
drift. Say where `baseline` is persisted. Baseline and live are both **file** facts; *what is
running* is a **daemon** fact and is not the same comparison.

### R2-5. `local/` is the bypass the refusal teaches. **[Tesla, Kelvin, Maxwell — all three]**

Round 1's disposition offered a directory **or** a hash. The fold did **both**. The hash watches
`current/`; `local/` sits outside it and survives every flip — and the section's stated reason
for existing is the 3am edit that the hash now correctly REFUSES. So the operator is refused,
then shown a directory the hash does not cover, whose membership rule is *"box-local state"* —
**the phrase §3 forbids as a membership rule.**

The first time a shipped compose file or `update.sh` references it (`env_file`, a bind, a
source), effective config is `cohort ⊕ local/`, **the drift predicate stays green, and "the
artifact in git IS the artifact on the box" is false again.** Nothing in CI forbids the
reference. Kelvin independently: *"An ambiguous state is a cold fault waiting for a phase
transition."* Separately: the manifest hash is stored in `GENERATION.txt`, **inside the tree
being hashed** — either always dirty, or excluded by an implementer with no spec.

**FOLD:** **delete the overlay.** One mechanism: the manifest hash REFUSES a modified
`current/`. Nothing in the shipped manifest may reference `local/`, **and CI greps for that
reference.** Record the hash outside the hashed bytes. If `local/` survives at all it is
scratch, not config, and the cohort cannot see it.

### R2-6. A denylist is closed only for the paths already imagined. **[Tesla, Carnot, Maxwell]**

`INCLUDE deploy/** minus a denylist` **defaults the next path to cargo.** The rule for the next
ambiguous directory is unwritten (§9.2 admits it). CI asserting the denylist stays green when
`deploy/keys/`, a second sops tree, or the shipper's real path appears — **the entry is still
the pronoun `<the shipper itself>`.** The first miss replicates to every island, is retained
for N generations, and **shred is specified only for `.env`.**

Tesla on its own round-1 words: *"I called the denylist 'the closed set' in round 1. That
phrase is the fossil."*

**FOLD:** replace the denylist with an **allowlist**. CI lists the tree that would ship and
**fails on any unclassified path** — unclassified is a red build, zero islands touched. Name
the shipper by path. Shred every secret-shaped file the cohort could have carried, not only
`.env`.

### R2-7. Break-glass requires the key whose loss is the emergency. **[Maxwell]**

§2 states that key loss blocks every config change, then specifies recovery as *"placing a
generation and swinging `current`"* — which contains `.env`, which needs decryption, which
needs the key. Hand-editing is what the next ship refuses. **The door was closed and no other
was opened.**

**FOLD:** specify break-glass for the keyless case, or state plainly that there is none and the
second recipient is the whole answer — a named risk with an owner, not a silent gap. Candidate:
allow a **signed, config-only generation** (compose + `deploy/`, no `.env`) so a compose forward
ships without touching secrets. *That is exactly the 2026-09-11 repair and it needs no key.*

### R2-8. "Mitigations" is the wrong heading. **[Kelvin]**

A second age recipient and provenance pinning mitigate **availability** and **operator error**.
Neither touches **compromise** — a compromised control side with a valid key and a clean
checkout unlocks every island. *"The entropy has not been reduced; it has been concentrated to
a critical mass."*

**FOLD:** rename to **Accepted Risks and Tradeoffs**, and say plainly that control-side
compromise is the central accepted risk of the push model.

## Carnot's DISSOLVE, recorded in full because it is the decision Nick may have to make

Not a kill at 1 family, but the strongest single argument in the round, and it must not be
filed as a dissent:

- **The economy test fails.** *"The design no longer resembles a small control-side shipper
  plus restore; it is a deployment subsystem. The added machinery is not accidental polish, it
  is required to make the subsystem safe enough to exist."*
- **The marginal gain is too narrow for the new trusted surface.** The existing preflight ran
  clean on both boxes on 2026-09-21; the remaining gap is `.env` plus avoiding a manual
  prelude — *"that does not justify centralizing decrypt authority and deployment authority
  into this much mechanism."*
- **Its alternative, which is narrower than option 1 and larger than option 3:** keep the
  preflight; build a **narrow `.env`-only tool** — decrypt one island's complete SOPS `.env`,
  compare to live, require explicit review, place mode-correctly, shred temps, **stop**. *"Do
  not combine it with executable deploy delivery."*

Maxwell reached the same escape hatch independently before reading Carnot's strike.

## What holds (unanimous across both rounds)

The cargo/vehicle cut. The subtractive middle — no template, no render, no byte-match proof.
One same-filesystem `rename(2)` as the right joint **for files** (only the operands are wrong).
The secret posture. `ship-release` pulling a control-side-resolved digest; `restore` refusing
across a digest boundary and printing the sqlite path; no image rollback in the tool; no
scheduler; the shipper never initiates. Cutover refusing a mismatched `.env`. Three outcomes,
fail closed. `aiko_data` external and outside the cohort. `standup.sh` box-resident. Option 3's
preflight is a real compiler and is not re-solved. And §2's honesty — *"do not decorate it back
into 'the loop is gone.'"*

Tesla on the human-compiler discharge, worth keeping as a constraint on round 3: **"Do not grow
a third prompt and call it a gate."**

## Disposition

**RECAST — round 2 of ≤3. One round remains.**

The eight folds above are specified and could be written. But two families independently set
the same bar for round 3, and it is not "fix these":

> **The next fold must be SMALLER than this one.** If R2-1 through R2-8 cannot be discharged
> without new machinery, the honest answer is **option 3** — one manual `deploy/` sync per box
> plus the preflight that already exists — or **Carnot's narrow `.env`-only tool**, which is
> the smallest thing that captures the gain that actually motivated this.

**Owed to Nick:** this is a second decision point, and it is his. Round 3 can be attempted, but
it should be attempted *only* as a subtraction. A round 3 that adds a ninth mechanism has
answered Carnot by proving him right.

---

# ROUND 3 — struck 2026-09-25, `dt-1790310533` — FINAL ROUND

**Overall verdict: NOT SOUND. 1 SOUND (Carnot) vs 3 RECAST (Maxwell, Kelvin, Tesla).**
Full panel, no dark seat.

**Round budget exhausted — ≤3 rounds, and this was 3.** Per `/design-temper`'s own rule the
disposition is **stop and report**, not a fourth fold. Status:
**UN-TEMPERED (provisional) — NOT BUILD-READY.**

**The subtraction was real. All four families said so**, including the three that withheld
SOUND. Three commands → one; `deploy/**` → three enumerated files; two acknowledgements → one;
digest-gated restore → plain restore; `local/` deleted; 284 lines removed against 226 added.
Carnot — who DISSOLVED round 2 on economy grounds — **reversed to SOUND** and recorded that its
economy test now passes: *"this is now a small control-side shipper plus restore… no longer
burning a boiler to move one valve."* It also affirmed §1's cohort-of-two argument over its own
`.env`-only alternative.

**What withheld SOUND is not size. It is that the central subtraction does not work.**

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Carnot | **SOUND** | Economy test passes; subtraction real, not cosmetic; cohort-of-two beats `.env`-only. |
| Maxwell | RECAST | `ISLAND_VERSION` cannot leave `.env` — **measured**: removing it unpins both islands to `edge`. |
| Kelvin | RECAST | The Genesis Barrier (`n=0` unreachable); the keyless ship is a void, not a subtraction. |
| Tesla | RECAST | *"The pin never leaves the only file Compose will read"*, and `update.sh` is asserted both ways and specified neither. |

**Carnot's SOUND rests on a premise the other three refuted**, and its own fold-back names the
hazard it missed: *"Do not let `ISLAND_VERSION` leak back through another env key or compose
substitution. Feynman's rule applies: the easiest person to fool is the one who renamed the
coupling."* It gave the pin's departure as its **reason** for SOUND.

## The three findings that end the round

### R3-1. The pin never left. **MEASURED.** [Maxwell, Tesla; Kelvin adjacent]

`docker-compose.yml:48,329,344` — `image: ghcr.io/…:${ISLAND_VERSION:-edge}`. That is **Compose
interpolation**, resolved from the process environment or **the project-directory `.env`**, and
`update.sh:90` runs `docker compose -f docker-compose.yml` with **no `--env-file`**.

Tesla's two horns, with no third:
- **The pin is in the shipped `.env`** → the shipper writes it, `restore` swings it, *"cannot
  change the image" is prose of the class this design quotes as its warrant*, and **the digest
  gate was deleted because of a departure that never happened.** `restore` of a generation
  whose `.env` names the old tag, followed by the operator's ordinary `update.sh`, pulls that
  tag onto an `aiko_data` possibly at schema *N+1* — **the recorded FATAL, reached by the
  recovery path**, with the sqlite path no longer printed because the boundary was declared
  uncrossable.
- **The pin is not in the shipped `.env`** → §7's full-byte cutover against the live `.env`
  (where ISL-0003 puts it) **refuses on every first ship**, and the next `up` interpolates an
  empty tag — i.e. **`edge`, which tracks `main`.** Both islands, silently, on the next
  `update.sh`.

Every escape reintroduces something deleted: preserve the box's line → **templating**, v1's
deleted fossil; export it → ISL-0003's measured hazard where an exported pin beats `.env` and
the guard blesses one tag while `pull` fetches another; `--env-file` → a second env source, the
precedence class this repo has already been bitten by.

**The one-command collapse, the unreachable-by-construction FATAL, and the ungated `restore`
all descend from this.** It does not hold, so they do not.

### R3-2. The keyless generation is §1 pulled to the opposite ground. [Kelvin, Tesla, Maxwell — all three]

§1's single sentence is that `.env` and `docker-compose.yml` **move together or not at all.**
§8 then ships compose and `mosquitto.conf` *without* `.env` and calls it the 2026-09-11 repair.

Kelvin's literal reading is worse than Maxwell's: `current` names a directory with **no `.env`
at all** — the old one sits in `releases/<previous>/`, which Compose does not walk back to. So
it is not a mismatch, it is a **void**: every variable unset. Tesla's charitable reading is the
mirror incident — *"the value was present and the forward was absent. Now the forward is
present and the value is absent. Same outage, halves swapped."* And it is **the path of least
resistance**, so it runs exactly when git is ahead of the box's secrets.

Kelvin's disposition, adopted: *"You cannot engineer your way out of a lost key with a tool
that requires the products of that key."* **There is no keyless ship.** Break-glass is the
second age recipient — a social process — which round 2 had already accepted as the whole
answer.

**Tesla also partly refutes §1's strong form**, and this is owed to Carnot: while
`preflight-compose-drift.sh` aborts `update.sh` before `up` when compose differs from the tag,
**a stale compose never becomes the incident** — so `.env`-alone is *not* still the September
incident. *"I am not dissolving the cohort for the overclaim. I am saying the overclaim is what
licensed the split, and the split is fatal."*

### R3-3. `update.sh` is asserted both ways and specified neither. [Tesla; Kelvin's Genesis Barrier is its `n=0` face]

The design says `update.sh` is **unchanged** *and* that generations are what gets applied.
Tesla: those cannot both be true.

- **It never looks at the generation** → the swing changes a name nothing reads, *"backups land
  where they land today" is true because the ship did not happen*, and **the ship is a no-op.**
- **It is pointed at the generation** → the project directory becomes `releases/<ts>`, **every
  relative bind moves with it**, and R2-2's backup arithmetic is back — with the inode gate
  deleted on the grounds that no new arithmetic existed.

*"The design asserts both directories and specifies neither invocation."*

**And the gap survives a reboot.** New compose, new `.env` on disk; old containers, old mounts,
old environment in the daemon; `/health` green about the old world — until something that can
see `current` actually runs. `restart: unless-stopped` restarts the **existing** container; it
does not re-read compose. Worse, the design's own discriminator inverts: *"GENERATION.txt is
newer than the running containers"* fails once a restart refreshes start times without
refreshing config — **the file becomes the older fact and the signal goes quiet.** The fact
that survives is the one §5a already queries: canonical `working_dir` of the running project
versus `realpath(current)`.

**Kelvin's Genesis Barrier is the same defect at `n=0`:** a fresh `standup.sh` box has
`aiko_data`, no containers, no `current/`. §5a reads that as *stopped tenant → REFUSE
`--adopt`*; §5 reads missing `current` as COULD NOT RUN. **The tool cannot adopt a box because
it is not already under management.** And `mv -T current.next current` when `current` is a real
directory rather than a symlink **fails the rename** — a case specified for the reverse
direction only, whose natural repair is the plain `mv` the design forbids.

## What holds (affirmed in all three rounds)

The cargo/vehicle cut — *"do not recast this back into a shim, a contract integer, or an image
entrypoint."* Three enumerated files and **no executables**; the denylist stays buried; the
human compiler is back to a diff the size of the incident that sized it. The swing for files:
staging under `REMOTE_PATH`, `mv -T` symlink-onto-symlink, plain `mv` forbidden. Three daemon
answers with the external volume as the tenant; `standup.sh` the only genesis. Secret posture
entire. No scheduler, no pull, no recreate in the shipper. The subtractive middle — **a
templating layer in any future fold is the fossil; kill it on sight.** `mosquitto.conf` earns
its seat (Tesla: *"a bind target of the compose you ship; leaving it outside would make the
cohort lie"*). And §1's sentence survives **as a constraint on any tool allowed to change
compose**, even though its strong form does not.

## Disposition — STOP. Two honest ways forward, and the choice is Nick's.

**A. Write Tesla's four sentences, then stop.** Tesla is explicit that its remaining fold-backs
are **not** a ninth mechanism: kill the keyless generation (no replacement); **name the one
`update.sh` invocation** — cwd, `-f`, `--project-directory` — plus the single inode assertion;
**stop saying the pin left** and restore the gate as one comparison (refuse when the running
tag and the live `.env` pin disagree; `restore` refuses across an `ISLAND_VERSION` difference
and prints the sqlite path); and **name the first ship** as its own transition gated by the §7
cutover diff alone. *"Write the four sentences. The next fold is smaller only if it is these,
and then stops."* — This is a round 4, which the budget forbids; it needs Nick's explicit
extension, not an inference.

**B. Take the honest remainder.** Tesla again: *"If 'update.sh unchanged' is kept as pride and
the project directory is left unnamed, the ship is a no-op and Carnot's `.env`-only tool is the
honest remainder — but that is you dropping the sentence, not me adding a machine."* That lands
on the narrow `.env`-only tool, or on **option 3** — one manual sync per box plus the preflight
that already exists and already works.

**Recorded so it cannot be lost either way:** `preflight-compose-drift.sh` is a real compiler,
it ran clean on both boxes on 2026-09-21, and three rounds of adversarial review have not found
a reason to re-solve it.

---

# ROUND 4 — struck 2026-09-25, `dt-1790347...` — budget extended by Nick

**Overall verdict: NOT SOUND. 1 SOUND (Carnot) vs 3 RECAST (Maxwell, Kelvin, Tesla).**
Full panel, no dark seat. **Same split as round 3.**

**What round 4 closed, and it closed cleanly.** Nick ruled *"the key can be the responsibility
of the operator"*, which **deleted** the keyless generation rather than fixing it, downgraded
the second age recipient to runbook advice, and put key availability out of scope. A
**measurement on both live boxes** — `docker compose` accepts repeated `--env-file` and merges,
2.40.3 and v5.1.0 — let `ISLAND_VERSION` move to operator-owned `pin.env`. Tesla: *"the pin
*can* live outside the generation with no templating, no export, and no second ship command…
The cargo/vehicle cut did not grow a component; it grew a flag."*

All three of Kelvin's round-3 findings: **DISCHARGED.** Two of Tesla's four fold-backs: **YES.**

**Carnot reversed to SOUND again** and answered the size question directly: *"Round 4 finally
pays in the right currency: deleted mechanisms, not prettier guards… Mechanism count is the
right primary measure here."* Its economy test passes.

**What withheld SOUND: three findings, and Maxwell/Kelvin/Tesla found the first two
independently.**

### R4-1. `pin.env` is a fourth box-resident file whose absence silently means `edge`

Nothing ships it, nothing syncs it, no preflight reads it, and the shipper deliberately does not
write it — while compose still says `${ISLAND_VERSION:-edge}`.

Tesla sharpened it past where Maxwell and Kelvin left it: **a missing `--env-file` target fails
closed (Compose errors — the kind default). An EMPTY file, or one with no `ISLAND_VERSION` key,
succeeds** — and interpolation falls to `edge`. *"Before this round that default was dead code
on a box whose `.env` always carried the pin. Round 4 made it the live fallback of a fourth
box-resident file that nothing ships, nothing hashes, nothing preflights, and nothing checks
for a key."* And: *"Nick's ruling gives the operator the pin; it does not license a default that
unpins."*

**DISPOSITION — a deletion, not a guard.** Remove `:-edge` from all three `image:` lines and use
`${ISLAND_VERSION:?…}`. An unset pin then errors at `config` time, before any pull, naming the
file. `edge` stays available by *writing* it — a choice, not an accident. *(Kelvin proposed
wrapping the invocation in an assert; Maxwell's and Tesla's removal is preferred, for Carnot's
stated reason: deleted mechanisms beat prettier guards.)*

### R4-2. §3d's cutover and §5b's first-ship gate are the same gate, inverted

§3d strips the pin from the `.sops` file **before** the first ship. §7/§5b refuse the first ship
unless decrypted sops bytes match the live `.env` (measured: pin still at `.env:12` enspyr,
`.env:14` imagineering). **So the required preparation is the refuse condition.**

Kelvin: *"The tool cannot adopt a box because the box has been correctly prepared for
adoption."* Tesla: *"Mid-cutover, 'done,' and 'box drift' are the same byte predicate. The tool
cannot distinguish them."* Maxwell added the ordering half: §4a's invocation references
`current/.env`, which does not exist before the first ship, so completing the cutover leaves the
island unable to deploy.

**DISPOSITION — Tesla's four-step fail-closed order, which also removes the exception Kelvin
proposed:** (1) write `pin.env` from the box's current pin; (2) install the §4a invocation;
(3) verify with **that** invocation that `docker compose config` resolves the **same** image tag
as before; (4) **then** remove the line from live `.env` *and* from sops — after which the
byte-diff is equal and the first ship proceeds with **no exception list**. *(Kelvin's
`grep -v '^ISLAND_VERSION='` before the diff is rejected: a byte-exact comparison with one
permitted exception is how v1's deleted field-by-field comparison returns.)*

### R4-3. The half-applied discriminator compares two names of the same live symlink — MEASURED, and it cannot work as written

**Tesla alone.** §4a passes `--project-directory "$REMOTE_PATH/current"`; §5 then asks whether
the daemon's canonical `working_dir` != `realpath(current)`. Tesla: *"One comparison, two
implementations, both wrong unless you have measured that Compose stores EvalSymlinks-at-`up`."*

**Measured 2026-09-25 on `chat.enspyr.co`**, with a throwaway project using exactly this shape
(`current -> releases/r1`, `--project-directory $PWD/current`):

```
com.docker.compose.project.working_dir  ->  /tmp/symtest/current
```

**The daemon records the symlink path, not the resolved release.** So:
- **realpath both sides** → both resolve through the *swung* symlink to the same place → they
  are **always equal** → half-applied is **invisible**, including across the reboot gap this
  predicate was written to catch.
- **do not realpath** → `.../current` != `.../releases/<ts>` → **always unequal** → every
  healthy box reads half-applied until the alarm is ignored.

**Tesla's prediction was exact and the measurement confirms it. The discriminator must be
replaced, not tuned** — it needs a daemon fact frozen at `up` that the swing cannot retarget.
`config_files` is not it (also `current/...`).

## The pattern, stated because it is now four rounds long

Each round's fix has created the next round's flaw, in the same shape: **the failure relocates
rather than resolves.**

| Round | Fix | Next round's flaw |
|---|---|---|
| 1 | three states (baseline/target/live) | baseline had no home |
| 2 | take `ISLAND_VERSION` out of the cohort | it cannot leave `.env` |
| 3 | `pin.env` | empty `pin.env` silently means `edge` |
| 4 | `--project-directory current` | the discriminator cannot work |

**The counter-reading is equally available and equally honest:** the findings are converging —
ten design flaws, then eight of mechanism, then three, then three of which two are one-line
deletions — and every round has discharged everything the prior round raised.

**Both readings feel identical from inside.** That is this project's standing crux, and it is
why the disposition below is Nick's and not the panel's.

## Disposition

**NOT SOUND at round 4 of an extended budget.** Options, stated without a recommendation
because the author has now said "almost there" four times:

- **Fold round 5.** R4-1 and R4-2 are deletions and an ordering; R4-3 needs a genuinely
  different mechanism and the measurement already says which family of answer is disqualified.
  Tesla's closing: *"Write the two predicates so they agree on the first ship, fail closed on
  an empty pin, and name an inode the swing cannot retarget — and then, this time, **stop**."*
- **Stop and fall back**, to Carnot's narrow `.env`-only tool or to option 3 — one manual sync
  per box plus the preflight that already exists, ran clean on 2026-09-21, and has survived
  four rounds of adversarial review without anyone finding a reason to replace it.

**One fossil to remove either way:** the word *"unchanged"* still sits in §2 describing
`update.sh`. Tesla: *"A fossil word here reopens R3-3 without another finding."*
