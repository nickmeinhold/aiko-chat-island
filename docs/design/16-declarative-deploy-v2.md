# Declarative island config delivery — design (#2301)

Status: **round 3, a subtraction** (2026-09-25). Owed a final strike; the built PR still owes a
`/cage-match`.

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

## 3. One command, and the FATAL becomes unreachable

**`ISLAND_VERSION` moves out of the cohort.** It lives on the box, operator-owned, in a file
the shipper does not write.

Round 2's R2-4 found that the pin living inside the shipped `.env` re-coupled the split:
`ship-config` was shut exactly when git was ahead of the box — *the normal state between bump
and release* — so the dangerous door became the only door. Taking the pin out does not repair
that. It **removes the condition**:

- The shipper **cannot change the image.** Not "refuses to" — cannot; it does not write the
  file that names it.
- Therefore it cannot trigger a migration. **The recorded FATAL is unreachable by this tool by
  construction**, rather than disarmed by a command split.
- Therefore `restore` cannot cross a digest boundary, and needs **no digest gate**.
- Therefore **`ship-config` / `ship-release` collapse into one command.**

Image upgrades continue to work exactly as ISL-0003 documents and as we did on 2026-09-21: bump
`ISLAND_VERSION` by hand, run `deploy/update.sh`. **Unchanged, already proven, not this
design's business.**

| | |
|---|---|
| **`ship <island>`** | place a generation, swing `current`. Never pulls, never recreates, never writes the pin. |
| **`restore <ts>`** | swing `current` back. Same operation, backwards. |

After either, the operator runs `update.sh` when they choose — the same human decision as
today, at the same moment.

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
  and must not be typed as it. `GENERATION.txt` is newer than the running containers; the
  shipper says so and offers continue-or-restore.

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
| inspect error | **COULD NOT RUN** |

**Absence of `current` is never genesis.** Genesis is `standup.sh`, once.

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

**Mitigated:**
- *Availability* — **a second age recipient**, so one laptop is not the only decrypt path.
- *Operator error* — the shipper **refuses a dirty tree** and stamps the **tree hash of the
  shipped manifest** into `GENERATION.txt`. One check, not a component. *(Kelvin's round-1
  `curl | bash` alternative stays **rejected**: remote code execution on the operator's
  machine is ISL-0003's founding objection to push-CD.)*

**Break-glass now works keyless, and this is a consequence of the subtraction rather than a
new mechanism.** Round 2 found that recovery required the key whose loss was the emergency.
With `.env` separable from the cohort, **a compose-only generation — `docker-compose.yml` plus
`mosquitto.conf`, both public — ships with no key at all.** That is *exactly* the 2026-09-11
repair: a one-line compose forward. The emergency path is the ordinary path minus one file.

**Unchanged and honest:** ISL-0003's limit is closed for operators who ship generations and
unchanged for those who do not. `standup.sh` remains irreducibly box-resident (#4749) — it
creates `aiko_data`, writes the genesis `.env`, and pulls the image, so at that moment there is
no generation. **No scheduler anywhere; the shipper never initiates.**

---

## 9. Round 2's eight flaws, and how this round answers them

| | Round 2 flaw | Answer | By |
|---|---|---|---|
| R2-1 | `mv -T` operands missing | §4 writes both commands; plain `mv` fallback forbidden | **repair** (2 lines) |
| R2-2 | backup gate a constant satisfies | shipper never invokes `update.sh`; no new path arithmetic exists | **deletion** |
| R2-3 | stopped project reads as missing | §5a, three daemon answers; `aiko_data` decides | **repair** |
| R2-4 | split re-couples via `ISLAND_VERSION` | pin leaves the cohort; one command; FATAL unreachable | **deletion** |
| R2-5 | `local/` is the bypass | deleted; hash alone | **deletion** |
| R2-6 | denylist defaults next path to cargo | three enumerated files; no `deploy/**` | **deletion** |
| R2-7 | break-glass needs the lost key | compose-only generation ships keyless | **deletion** |
| R2-8 | "Mitigations" mis-titled | §8 renamed; compromise stated as accepted | **repair** |

**Five deletions, three repairs, no new mechanism.** Against round 2: three commands → one;
`deploy/**` + governed manifest → three names; two acknowledgements → one; digest-gated restore
→ plain restore; `local/` → gone.

## 10. What the final strike should hit

1. **Is the cohort-of-two argument sound?** §1 claims `.env`-alone reproduces 2026-09-11
   because the value and its forwarding live in different files. If that is wrong, Carnot's
   narrow tool wins and this design should dissolve.
2. **Does `ISLAND_VERSION` actually leave cleanly**, or does something else in the shipped
   `.env` couple to the image version in a way that reintroduces R2-4?
3. **Is `mosquitto.conf` earning its place**, or is it a third file smuggled in by symmetry?
   The 2026-09-11 incident involved two.
4. **Does §5's all-file-facts comparison really fix R2-4's half-swing**, or does "placed but not
   `up`" still have a state the tool mis-types?
5. **The economy test, final call.** One command, three files, no executables, no scheduler, no
   pull. Is this now *"a small control-side shipper plus restore"* — or is even this more than
   `.env` is worth?
