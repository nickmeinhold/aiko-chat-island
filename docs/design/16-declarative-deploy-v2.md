# Declarative island config delivery — design (#2301)

> ## ROUND 4 STRUCK — NOT SOUND (1 SOUND / 3 RECAST). DO NOT BUILD FROM THIS FILE.
>
> Nick's key ruling and the `--env-file` measurement **closed two of round 3's three findings
> cleanly** — the keyless ship is deleted, all of Kelvin's round-3 findings discharged, and
> Carnot reversed to SOUND: *"round 4 finally pays in the right currency: deleted mechanisms,
> not prettier guards."* Three findings withheld it. Full record: **[16-TEMPER.md](16-TEMPER.md)**.
>
> - **R4-1** — `pin.env` is a fourth box-resident file nothing ships, syncs or checks, and an
>   **empty** one succeeds while interpolation falls to **`edge`, which tracks `main`**. A
>   missing file fails closed; an empty one does not. Fix is a deletion: `${ISLAND_VERSION:?}`.
> - **R4-2** — §3d's cutover strips the pin from sops *before* the first ship, and §7 refuses
>   the first ship unless sops matches the live `.env`. **The required preparation is the
>   refuse condition.**
> - **R4-3 — MEASURED, and it cannot work as written.** §5's half-applied discriminator
>   compares `working_dir` to `realpath(current)`. On `chat.enspyr.co` the daemon records
>   **`/tmp/symtest/current` — the symlink, not the release.** So realpath-both → always equal
>   → half-applied invisible; realpath-neither → always unequal → every healthy box alarms.
>   Tesla predicted exactly this; the measurement confirms it.
>
> **Also: the word "unchanged" still sits in §2 describing `update.sh` — a fossil that reopens
> R3-3.** §4a is the truth; §2 is the leftover.

Status: **NOT SOUND — round 4 struck** (2026-09-25).

**Nick's pick 2026-09-24 (#4750, option 1)**, then two temper rounds:
[16-TEMPER.md](16-TEMPER.md), strikes in `16-strikes/`. Round 1: RECAST 4/4, ten flaws of
design. Round 2: RECAST 3, **DISSOLVE 1**, eight flaws of mechanism — and both Carnot and Tesla
set the same bar for this round:

> **The next fold gets smaller. A round 3 that adds a ninth mechanism has answered Carnot by
> proving him right.**

**This round removes rather than repairs.** Three commands become one. `deploy/**` plus a
governed manifest becomes three enumerated files. Two acknowledgement classes become one.
Digest-gated `restore` becomes plain `restore`. `local/` is deleted. **Six of round 2's eight
flaws are discharged by deletion, not by guard.**

The name changed with the scope: this ships **config**, not deploy tooling.

> **DELETED AND MUST NOT RETURN.** No `.env.template`, `envsubst`, render step or
> byte-match-as-proof (v1). No shim, contract integer or image entrypoint (round 1) — *"that
> corpse stays buried."* No `local/` overlay, no denylist governance, no second ship command,
> no executable cargo (round 3). Any of these reappearing is a fossil.

---

## 1. The problem, and the one sentence that scopes this design

On 2026-09-11 `chat.enspyr.co` went down for several minutes. `APNS_VOIP_TOPIC` became
required; the box's **`.env` had it**; the box's **`docker-compose.yml` did not forward it**.
One line of difference. imagineering had the identical gap and deployed clean because its
compose had been hand-copied more recently.

ISL-0003 names the class: **prose functioning as executable governance with no compiler.**

**What already works and must not be re-solved.** `preflight-compose-drift.sh` **refuses** on
compose and `deploy/` drift, before the backup and before anything is pulled. It is a real
compiler; it ran clean on both boxes on 2026-09-21. Round 2's DISSOLVE rested on this and it
is correct.

**What it structurally cannot do**, and the whole remaining justification:

> `.env` holds per-box secrets and **cannot be compared against a public tag.** It is invisible
> to every existing guard. And **`.env` and `docker-compose.yml` are the pair that broke** —
> the value was in one and the forwarding was in the other.

**Therefore they must move together or not at all.** That is the entire design.

It is also why Carnot's `.env`-only tool — the round-2 DISSOLVE's alternative — is not quite
right: shipping `.env` alone, against a compose file that arrived separately, **reproduces the
exact shape of 2026-09-11.** A cohort of two is the smallest thing that cannot.

---

## 2. What is shipped: three files, enumerated

```
docker-compose.yml
mosquitto.conf
.env                 # decrypted from deploy/secrets/<island>.env.sops
```

**That is the complete list.** Not a pattern, not a quantifier minus a denylist — three names.

**`deploy/**` is NOT shipped.** Round 2's R2-6 said a denylist *"defaults the next path to
cargo"* and Tesla called its own round-1 phrase *"the denylist is the closed set"* a fossil.
The subtraction dissolves the governance problem rather than solving it: with three enumerated
files there is no next ambiguous directory, no allowlist CI to maintain, no `<the shipper
itself>` pronoun, and **nothing executable is shipped at all.**

`deploy/` keeps arriving by hand — **guarded, loudly, by the preflight that already refuses on
its drift.** That is a capability we have. `.env` is the one we do not.

**Consequences of shipping no executables, all of them deletions:**

- **R2-7's two acknowledgement classes: DELETED.** There are no `deploy/**` hunks to
  acknowledge separately, because there are none. Tesla: *"do not grow a third prompt and call
  it a gate"* — this grows zero. One review of a three-file diff, which is the size the human
  compiler was sized for in 2026-09-11.
- **R2-2's backup-path problem: DISSOLVED.** It arose because `update.sh` would run from inside
  a generation and Compose resolves binds from the canonical project directory. **The shipper
  never invokes `update.sh`.** `update.sh` stays where it is, box-resident, unchanged, run by
  the operator exactly as ISL-0003 documents. Backups land where they land today. There is no
  new path arithmetic, so there is no gate to write and no gate that a constant could satisfy.
- **The daemon-root delivery concern is gone.** Nothing the shipper places executes.

---

## 3. The pin leaves the cohort — measured, not asserted

**`ISLAND_VERSION` lives in `pin.env`: operator-owned, outside every generation, never written
by the shipper.**

`docker-compose.yml:48,329,344` read `${ISLAND_VERSION:-edge}` — Compose **interpolation**,
resolved from the process environment or the project-directory `.env`. Round 3 found the pin
therefore could not leave, because `update.sh:90` passed **no `--env-file`** and Compose would
fall through to the default: **`edge`, which tracks `main`.**

**Measured on both live boxes, 2026-09-25:**

```
enspyr        Docker Compose 2.40.3
imagineering  Docker Compose v5.1.0
both:  docker compose -f … --env-file a.env --env-file b.env config
       ->  image: busybox:one-two
```

**Compose accepts repeated `--env-file` and merges them.** So the pin leaves the shipped file
and is still interpolated — via a second source the shipper does not write.

**The cost is one line**, and it is the line Tesla's round-3 fold-back demanded be written down
regardless (see §4a). It is **not** any of the three escapes round 3 ruled out: it is not
templating (v1's deleted fossil), not an exported variable (ISL-0003's measured hazard where an
exported pin beats `.env` and the guard blesses one tag while `pull` fetches another), and not a
replacement of `.env` by a different single file.

**Now the claim is true rather than prose:**

- The shipper **cannot change the image.** It does not write `pin.env`. Not "refuses to."
- Therefore it cannot trigger a migration. **The recorded FATAL is unreachable by this tool.**
- Therefore `restore` cannot move the pin either — it swings generations, and the pin is not in
  one. **No digest gate**, and this time for a reason that holds.
- Therefore **one command**, not two.

**Kelvin's Ghost, closed by assertion rather than by trust.** Round 3's Gemini strike noted the
pin could return through *any* variable in an `image:` key. **CI asserts that the only variable
appearing in any `image:` key is `ISLAND_VERSION`** — a grep-shaped test over the compose file
in the repo. Anything else is a red build.

Image upgrades are unchanged and stay the operator's: edit `pin.env`, run `deploy/update.sh`.
Exactly as ISL-0003 documents and as we did on 2026-09-21.

| | |
|---|---|
| **`ship <island>`** | place a generation, swing `current`. Never pulls, never recreates, **never writes `pin.env`**. |
| **`restore <ts>`** | swing `current` back. Same operation, backwards. |

### 3d. Cutover: the pin must be lifted out of the committed `.env` first

`deploy/secrets/<island>.env.sops` was a **verbatim** lift of each box's `.env` on 2026-09-06,
and the pin is in there — measured on the boxes today at `.env:12` (enspyr) and `.env:14`
(imagineering).

**One-time, before the first ship, per island, in this order:** write `pin.env` on the box
carrying the island's *current* `ISLAND_VERSION`; remove that line from the island's `.sops`
file; update `update.sh`'s invocation (§4a). **Verify by reading `docker compose … config` and
confirming the resolved image tag is unchanged** — an affirming check, not a grep for absence.
Until all three are done for an island, that island is not shippable, and the tool refuses it
rather than assuming.

---

## 4. Placement and the swing

```
<REMOTE_PATH>/
  releases/<ts>/{docker-compose.yml, mosquitto.conf, .env, GENERATION.txt}
  current -> releases/<ts>
  backups/          # untouched by this design; update.sh owns it, as today
```

- Staging **mandatorily under `REMOTE_PATH`**, never `/tmp` — `/tmp` turns `mv` into
  copy+unlink and reopens the truncation class that caused the original incident.
- Fail if `releases/<ts>/` already exists.
- **The swing, written as two commands because round 2 found the flag alone does not work:**

  ```sh
  ln -s "releases/$TS" current.next
  mv -T current.next current          # symlink onto symlink: one rename(2)
  ```

  **`mv -T releases/$TS current` is a specified failure, not an implementation choice** —
  source is a directory, destination is a symlink, and `mv` refuses. Its natural repair is
  dropping `-T`, which **nests the new generation inside the old one**: the defect `-T` exists
  to prevent. **Fail closed if `mv` has no `-T`. Never fall back to plain `mv`.**

### 4a. The one invocation, written down

Round 3's third finding was that this design asserted `update.sh` was **unchanged** *and* that
generations are what gets applied — *"those cannot both be true."* Either it never reads the
generation, in which case **the ship is a no-op**, or it does, and the project directory becomes
`releases/<ts>` and every relative bind moves with it.

**It is not unchanged. The delta is one line, and here it is:**

```sh
docker compose \
  --project-directory "$REMOTE_PATH/current" \
  -f "$REMOTE_PATH/current/docker-compose.yml" \
  --env-file "$REMOTE_PATH/current/.env" \
  --env-file "$REMOTE_PATH/pin.env" \
  up -d
```

`-f` stays pinned for the reason it was pinned (measured: `compose.yaml` beside
`docker-compose.yml` wins outright, an override merges, `COMPOSE_FILE` redirects the set).

**And the assertion round 3 said was deleted on a false premise is now writable, because the
project directory is finally named:** a CI test runs the real backup path with *this*
invocation and asserts **the canonical parent of the written inode** is `<REMOTE_PATH>/backups`,
outside `releases/`. **A string helper must not be able to pass it** — round 2's gate could be
satisfied by `printf`, which is why this is specified as an inode and not a path.

`<REMOTE_PATH>/backups` is the single path exempted from §6's outside-the-generation grep.
**One path, named — not a prefix and not a class.**

**What the swing does and does not do** — round 1's overclaim, kept deleted:

> The swing atomically changes **which files `current` names**. Running containers change only
> when the operator runs `update.sh`. **Two facts on disk — placed, and running-as-of-last-`up`
> — and incident response must be told which one it is reading.**

`GENERATION.txt` records **what was placed**: git sha, tree hash of the shipped manifest,
island, operator, UTC. It does **not** claim to say what is running; ISL-0003's rule stands
unamended — *verify the running container, not the doc.*

---

## 5. Refuse and show

**Three states** (Carnot, round 1), with round 2's correction that they must all be facts of
the same kind:

| | |
|---|---|
| **baseline** | the tree hash in the box's `current/GENERATION.txt` — **a file fact, read from the box** |
| **target** | the generation being shipped |
| **live** | the hash of `current/`'s actual bytes, recomputed now |

Round 2 found `baseline` had no home and that the comparison mixed a process-belief with a
directory. **Both are now read from the box and both are file facts.** The operator's belief is
not an input.

- **REFUSE when `live != baseline`** — someone changed `current/` after it was placed. That,
  and only that, is drift. *(This also deletes `local/`'s reason to exist — see §6.)*
- **SHOW `target - baseline`** as the intentional change. Three files. Do not call it drift.
- A **half-applied state** — swing landed, operator has not run `update.sh` — is **not drift**
  and must not be typed as it.

  **The discriminator is not a timestamp, and round 3 is why.** *"`GENERATION.txt` is newer
  than the running containers"* inverts the moment any restart refreshes start times without
  refreshing config — and `restart: unless-stopped` restarts the **existing** container without
  re-reading compose, so **the gap survives a reboot** and the signal goes quiet exactly when it
  is needed. **Half-applied is: the running project's canonical `working_dir` !=
  `realpath(current)`.** That is a fact the daemon keeps, it is the same query §5a already
  makes, and no restart can launder it.

**Outcomes:** REFUSED (non-zero, diff on stderr, box untouched) / PASSED (zero) / **COULD NOT
RUN (non-zero, fail closed)** — ssh failed, `current` missing or dangling, island unidentified,
or **any daemon inspect error**. A check that could not run is not a check that passed.

### 5a. Tenant identity: three daemon answers, not two

Both boxes are shared. Round 2 found that a **stopped** project and a **missing** project gave
the same signal, and that signal permitted `--adopt` onto a surviving volume.

Compose project identity is **container labels**; default queries list only running projects,
and `compose down` removes the labels — **but not the external volume.** `aiko_data` survives,
and `aiko_data` is the tenant.

Query `docker ps -a` / the labels, never default `compose ls`:

| Daemon says | Action |
|---|---|
| labels present, `working_dir` under this `REMOTE_PATH` | **proceed** |
| labels present, path elsewhere | **REFUSE** — wrong tenant |
| **no containers, but external volume `aiko_data` exists** | **REFUSE** — this is a *stopped tenant*, not an absence |
| no containers, no `aiko_data` | absence. `--adopt` permitted (explicit flag only) |
| **containers present, no `GENERATION.txt`** | **FIRST SHIP** — its own state, see 5b |
| inspect error | **COULD NOT RUN** |

**Absence of `current` is never genesis.** Genesis is `standup.sh`, once.

### 5b. The first ship is its own state — Kelvin's Genesis Barrier

Round 3's Gemini strike found that the two live islands were an **unnamed state**, and worse, an
unreachable one: a box with containers running but no `current/` trips *"missing `current` =
COULD NOT RUN"*, while a freshly stood-up box with `aiko_data` and no containers trips *"stopped
tenant = REFUSE `--adopt`"*. **The tool could not adopt a box because the box was not already
adopted.**

A first ship is **containers present, `working_dir` under this `REMOTE_PATH`, no
`GENERATION.txt`.** It is:

- **not** gated by `live != baseline` — there is no baseline yet, and demanding one is the
  Catch-22;
- **not** `--adopt`, which means *"no island here"*, a different claim entirely;
- gated by **§7's cutover diff alone**: decrypted sops bytes against the live `.env`, REFUSE on
  mismatch. That gate exists precisely to stop a first flip consecrating box drift or
  clobbering production secrets, and round 3 found it was never reached.

**And the swing itself differs on a first ship**, which round 3 also caught: `current` is not
yet a symlink, so `mv -T current.next current` onto a **real directory** fails the rename — a
case §4 specified only in the reverse direction. It **fails closed with a named error** and a
specified rename-aside of the existing tree. **It never falls through to the plain `mv` §4
forbids.**

---

## 6. `local/` is deleted

Round 1 offered a directory **or** a hash for box-local state; round 2's fold took both, and
all three adversary families found the result. The overlay sat outside the hash, survived every
flip, and the first shipped compose referencing it would make effective config `cohort ⊕
local/` **with the drift predicate still green.**

**One mechanism: the manifest hash in §5 REFUSES a modified `current/`.** A 3am edit is caught,
not orphaned, and the operator is shown what they changed.

- **Nothing in the shipped manifest may reference a path outside the generation**, and **CI
  greps for that**. Three files makes this checkable by reading.
- The tree hash is stored in `GENERATION.txt`, so `GENERATION.txt` is **excluded from the
  hashed set** — stated here because round 2 caught the hash being stored inside the bytes it
  hashes.

---

## 7. Secrets

- **`sops -d` never reaches a shell environment.** Decrypt to a `umask 077` **file**; never
  `export`; `set +x` around the region. `/proc/<pid>/environ` is readable, children inherit,
  argv is visible, and `trap` does not run on SIGKILL.
- **`<island>.conf` parsed as DATA, never sourced.** Strict `KEY=VALUE` allowlist; validate
  `REMOTE_PATH` and `SSH_ALIAS`.
- **Shred both ends** — the box's temp *and the laptop's*.
- **Modes at creation, not chmod-after:** generation `0700`, `.env` `0600`.
- **Retention:** keep N (3). Never unlink the target of `current`. **Shred `.env` before
  removing a generation.** With three files, `.env` is the only secret-shaped one — which is
  checkable by reading rather than by maintaining a list.
- **Cutover (v1's temper, restored):** before the first flip, `diff` the decrypted sops bytes
  against the live `.env` and **REFUSE on mismatch.** Neither consecrate box drift nor clobber
  production secrets.

---

## 8. Accepted risks and tradeoffs

*(Renamed from "Mitigations" — Kelvin, round 2: a second recipient and provenance pinning
address availability and operator error. **Neither touches compromise.** Calling them
mitigations was the wrong word for the wrong risk.)*

**Accepted, central, and not mitigated:** a compromised control side, holding a valid key and a
clean checkout, can write config to every island it ships. The push model concentrates that
authority. This is the cost of the model, stated rather than engineered around.

**Out of scope, by Nick's ruling 2026-09-25:** *availability of the operator's own key.* Key
custody is the operator's, like their ssh key. The design does not engineer around losing it —
see the break-glass paragraph below, which is why the keyless generation is gone.

**Mitigated:**
- *Operator error* — the shipper **refuses a dirty tree** and stamps the **tree hash of the
  shipped manifest** into `GENERATION.txt`. One check, not a component. *(Kelvin's round-1
  `curl | bash` alternative stays **rejected**: remote code execution on the operator's
  machine is ISL-0003's founding objection to push-CD.)*

**Break-glass for a lost key is a SOCIAL process, and there is no keyless ship — Nick,
2026-09-25: *"the key can be the responsibility of the operator."***

Round 3 had a compose-only generation that shipped without `.env`, sold as the 2026-09-11
repair. All three adversary families killed it, and Kelvin's reading was the worst: `current`
would name a directory with **no `.env` at all** — not a mismatch but a **void**, every variable
unset. Tesla's was the mirror: *"the value was present and the forward was absent. Now the
forward is present and the value is absent. Same outage, halves swapped."* And it was **the path
of least resistance**, so it would run exactly when git was ahead of the box's secrets.

It also broke §1, which is this design's one sentence: **`.env` and `docker-compose.yml` move
together or not at all.** A mechanism that splits the pair to work around a lost key contradicts
the reason the pair exists.

**Deleted, not replaced.** Kelvin's disposition, now ratified: *"You cannot engineer your way
out of a lost key with a tool that requires the products of that key."* **A ship carries all its
files or does not happen.**

Consequently the **second age recipient is advice to an operator, not a requirement of this
design.** It was a mitigation for key loss; key custody is now out of scope. Recommend it in
the runbook; do not build around its absence.

**Unchanged and honest:** ISL-0003's limit is closed for operators who ship generations and
unchanged for those who do not. `standup.sh` remains irreducibly box-resident (#4749) — it
creates `aiko_data`, writes the genesis `.env`, and pulls the image, so at that moment there is
no generation. **No scheduler anywhere; the shipper never initiates.**

---

## 9. Round 3's three findings, and how round 4 answers them

| | Round 3 finding | Answer | By |
|---|---|---|---|
| R3-1 | the pin cannot leave `.env`; removing it unpins both islands to `edge` | repeated `--env-file`, **measured on both boxes**; `pin.env` is operator-owned and never shipped | **measurement + one line** |
| R3-2 | the keyless generation is §1 inverted — a void, not a subtraction | **deleted, not replaced.** Key custody is the operator's (Nick, 2026-09-25) | **ruling** |
| R3-3 | `update.sh` asserted unchanged *and* required to apply generations | §4a writes the invocation; the inode assertion becomes writable because the project directory is finally named | **the same one line** |

Carried with them, from round 3's smaller findings: **Kelvin's Genesis Barrier** (§5b — the
first ship is its own state, gated by the cutover diff alone), **the reboot-survives-the-gap
discriminator** (§5 — canonical `working_dir` vs `realpath(current)`, never a timestamp), the
**first-ship swing** onto a real directory failing closed rather than falling through to plain
`mv` (§5b), **Kelvin's Ghost** closed by a CI assertion that `ISLAND_VERSION` is the only
variable in any `image:` key (§3), and the **one-time pin cutover** (§3d).

**Is round 4 smaller? Split the question, because the two answers differ and the honest one is
not the flattering one.**

**Mechanisms: fewer.** One emergency mode deleted (keyless ship). One required mitigation
downgraded to advice (second age recipient). One key ceremony moved out of scope. Added: a
Compose flag, an invocation that was always implicitly required, and a CI assertion. **No new
component.**

**Prose: LONGER — 302 lines to ~430.** That is a real reversal of round 3's direction and it is
stated rather than buried. The growth is `.env`-cutover procedure (§3d), three
previously-unnamed states (§5b), and the written-out invocation (§4a). Some of it is history
that belongs in `16-TEMPER.md` and not here; a reader should judge whether §3d in particular is
a paragraph or a migration project, which is why §10.5 puts exactly that to the strike.

**The claim this design makes is therefore narrow: the mechanism count fell and the page count
rose.** If the strike finds the page count is the honest measure, that is a finding, not a
defence I have already prepared against.

## 10. What the final strike should hit

1. **The `--env-file` claim is measured on today's boxes** (2.40.3 and v5.1.0). Is it a stable
   contract or a version-dependent behaviour? What happens on a third-party island running an
   older Compose — and does the tool detect that, or unpin them to `edge` silently?
2. **§3d's cutover has an ordering.** `pin.env` written, sops line removed, invocation updated.
   What is the state between steps, and does the tool refuse an island mid-cutover as claimed?
3. **§5b names the first ship — is it now reachable?** Trace both live boxes and a fresh
   `standup.sh` box through §5a's table and §5b together, and check that no box lands in two
   rows or none.
4. **The half-applied discriminator is now a daemon fact.** Does `working_dir !=
   realpath(current)` have a false positive — e.g. an operator who legitimately ran compose from
   elsewhere once?
5. **Has the subtraction held?** Round 4 removed an emergency mode and a key ceremony and added
   a flag. Is that genuinely smaller, or is the one-time cutover (§3d) a migration project
   wearing a paragraph?
6. **Carnot's economy test, one last time.** One command, three files, no executables, no pull,
   no key ceremony — with a one-time pin migration. Does it still pass?
