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
