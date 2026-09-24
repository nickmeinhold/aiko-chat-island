# Declarative island deploy v2 — design (#2301)

Status: **design, not build-ready** (2026-09-24). Owed a `/design-temper` before any code,
per v1's own closing rule: *"Do not mark v2 'battle-tested' on the strength of v1's strike."*

**Nick's pick, 2026-09-24 (#4750, option 1)**, after #4684 — move deploy tooling into the
attested image — was **DISSOLVED 4/4** at temper. That panel's own alternative, reached
independently from four directions, is this document.

Supersedes `docs/crucible/47-declarative-deploy/DESIGN.md`, which has carried a superseded
banner since its temper deleted its central layer. That banner exists because two families
warned in the PR#166 cage-match that **a stale design beside a live artifact is how a
deleted layer gets rebuilt with production credentials in it.** This document is the fix
for that, and the first thing it does is refuse to rebuild the layer.

> **DELETED IN v1's TEMPER — DO NOT REINTRODUCE.** There is no `.env.template`, no
> `envsubst`, no render step, no missing-var map, and no byte-match gate. Finding 7, the
> *subtractive middle*: store the **complete** encrypted `.env` per island. *"The artifact
> in git IS the artifact on the box."* If a future revision of this design grows a
> templating layer, that is the fossil returning and it should be struck on sight.

---

## 1. The problem, stated as a property rather than an incident

The island is two halves that update by different mechanisms. **Half one — the image —
updates itself**: pinned by `ISLAND_VERSION`, pulled from a registry, exact. **Half two —
`docker-compose.yml`, `deploy/` (2,373 lines), `.env` — arrives by hand**, and drifts.

On 2026-09-11 that cost `chat.enspyr.co` several minutes: `APNS_VOIP_TOPIC` became
required, the box's `.env` had it, the box's **compose did not forward it**, and the island
refused to boot. The box's compose differed from the tag by exactly one line. imagineering
had the identical gap and deployed clean, because its compose had been hand-copied more
recently — same change, same drift, opposite outcome, **one variable: which files the
operator happened to be thinking about.**

ISL-0003's amendment names the class: **prose functioning as executable governance with no
compiler.**

**What is already fixed, and must not be re-solved here.**
`deploy/preflight-compose-drift.sh` now **refuses** a deploy on drift, before the backup and
before anything is pulled, with the diff on stderr. That is a real compiler and it works —
it ran clean on both boxes on 2026-09-21. Option 3 on #4750 (*accept the limit; the guard
exists*) was a genuinely close call for exactly this reason.

**What remains, and is the only thing this design is for:** the guard lives in `deploy/`,
which is the half that does not sync. A box whose copy predates the guard does not run it.
**The detector cannot move the thing it detects.** Design 15 tried to close that by
shrinking the unsynced surface; it dissolved because shrinking is not removing.

> **The whole thesis in one line:** stop detecting drift, and remove the thing that drifts.
> A cohort that arrives complete or not at all has nothing to compare against itself.

---

## 2. The one question v1's temper never had to ask

v1 assumed delivery was box-side. This design's central mechanism turns on a distinction
neither v1 nor design 15 stated, so it gets its own section.

**Design 15 died on a bootstrap loop: box-resident code was needed to *fetch* the new
code, so the fetcher could never update itself.** Every arrangement reduced to old code
gating new code, or new code certifying itself.

**That loop does not exist here, because delivery is a PUSH.** The control side — an
operator's own machine, holding the age key — already has everything: the repo, the
decryptable secrets, ssh to the box. It does not need to ask the box for anything in order
to *place* a generation. So:

| | Design 15 (dissolved) | This design |
|---|---|---|
| Who initiates | box-resident shim | the operator, control-side |
| What must already be on the box | the shim, which never syncs | **ssh + docker. Nothing of ours.** |
| Can the deploy tooling update itself | **no** — the fetcher can't fetch itself | **yes** — it arrives as cargo, not as the vehicle |

**`deploy/` is inside the generation.** The scripts that run a deploy are delivered *by*
the deploy, from outside, by something that was never on the box. That is what closes
ISL-0003's limit rather than relocating it — and it is available only to a push, which is
why option 1 works where option 2 could not.

**The cost, named up front:** this only helps an operator who uses the control-side path.
See §8 — it is the honest limit and it is smaller than it looks.

---

## 3. What a generation is

A **generation** is every box-resident file the repo is authoritative for, as one
addressable unit:

```
<REMOTE_PATH>/
  releases/
    20260924-233000/          # generation, named by UTC timestamp
      docker-compose.yml
      deploy/                 # ALL of it — update.sh, the preflights, lib/
      mosquitto.conf
      .env                    # decrypted from deploy/secrets/<island>.env.sops
      GENERATION.txt          # git sha, tag, island, operator, UTC, image digest
  current -> releases/20260924-233000
  backups/                    # untouched; update.sh's own DB backups live here
```

**In the generation:** compose, the whole of `deploy/`, mosquitto config, `.env`.
**Not in it, ever:** `aiko_data` (an external named docker volume — config replacement must
never be able to reach the store), `backups/`, and anything the operator legitimately keeps
locally.

`GENERATION.txt` exists so **the box can answer "what am I running?" without the control
side** — the question ISL-0003 says a pinned tag exists to make answerable, extended from
the image to everything around it.

### 3a. `.env` is a decrypted artifact, and it is the reason this is worth doing

`.env` cannot be drift-checked. `preflight-compose-drift.sh` compares the box against the
public tag and **`.env` is out of scope by construction** — it holds per-box secrets. It is
the one file no existing guard can reach, and it is half the config surface.

Shipping the whole generation covers it because the control side holds the age key. That is
the capability option 3 does not have, and the concrete reason to prefer option 1 over
leaving the limit standing.

---

## 4. Atomicity — the finding that killed v1's second-biggest assumption

v1 staged files and `mv`'d them. All three families rejected it: **`rename(2)` is
per-inode**, so a multi-file `mv` is not a cohort operation, and a crash leaves mixed-
generation config. Tesla's kicker: **staging must be same-filesystem** — staging in `/tmp`
turns `mv` into copy+unlink, reopening the truncation class that caused the original
incident.

**The design: write the whole generation into `releases/<ts>/`, then flip ONE symlink.**

- The generation is assembled under `<REMOTE_PATH>/releases/` — **mandatorily the same
  filesystem**, never `/tmp`. Non-negotiable; a build that stages elsewhere is wrong.
- `current` is flipped by a single atomic `rename(2)` over the symlink. One inode, one
  syscall. Before it, nothing is live; after it, everything is.
- **Rollback is the same operation backwards:** `restore <ts>` flips `current` to an earlier
  generation. Generation-addressed, so independent `.bak` files can never recombine halves
  of two generations — v1's flaw 5.

**What an atomic flip does NOT give you, stated because v1 was punished for implying it
did:**

- **It is not a rollback of the database.** The recorded FATAL stands: image rollback is not
  database rollback. Flipping `current` back does not un-migrate `aiko_data`. See §7.
- **It is not proof the new config is correct.** `/health` passes on wrong-but-alive config —
  a bad `PASSKEY_RP_ID` serves happily and breaks passkeys. Auto-rollback cannot catch
  silent misconfiguration and this design does not claim to.

---

## 5. Refuse-and-show, control-side

ISL-0003's posture is deliberate and survives verbatim: **refuse and show the diff; never
auto-sync.** A box may legitimately carry local state, and silently clobbering it is how
#2301 became standing instead of caught. Refusing puts a human in the loop for one line of
diff.

What changes is only *where* it runs. Today the box fetches the tag over codeload and
compares locally. Here the control side reads the box's current generation, diffs it against
the generation about to ship, prints it, and **refuses before shipping** — without executing
the image, and without needing anything of ours already on the box.

**Three outcomes, never two** — carried unanimously out of design 15's dissolve:

| Outcome | Meaning | Exit |
|---|---|---|
| **REFUSED** | compared, and said no | non-zero, diff on stderr, box untouched |
| **PASSED** | compared, and affirmed | zero |
| **COULD NOT RUN** | ssh failed, `current` unreadable, island not identified | **non-zero** |

COULD NOT RUN **fails closed**. A check that could not run is not a check that passed — this
project's own predicate/lifecycle lesson, and the arm that existing tooling currently gets
wrong by warning and continuing.

### 5a. Tenant preflight — verify the island before overwriting it

v1's flaw 6: confining *writes* to `REMOTE_PATH` does not confine *effects*, and there was no
check that `REMOTE_PATH` is the island you think it is. Both boxes are shared — imagineering
runs dreamfinder/lyra and a separate `matrix-*` stack; enspyr runs real human tenants.

**Before any write:** read the box's `current/GENERATION.txt` (or `.env` on first adoption)
and confirm the island identity matches the generation being shipped. Mismatch is REFUSED,
not a prompt.

This is the sibling of a hazard measured on 2026-09-24 from the app side: *a zero from the
wrong island is indistinguishable from a working feature nothing has exercised.* Two
production instances plus an operation that does not name its target is a standing trap.
**Name the target and verify it.**

### 5b. Compose is a privilege boundary, not a config file

`docker compose` under `sudo -n docker` on enspyr is daemon-root authority. Per-app scope
only: project name pinned `aiko`, `-f docker-compose.yml` pinned (which already ended a
class of fail-open — `compose.yaml` silently wins over `docker-compose.yml`), no prune, no
host restart, no external network edits.

---

## 6. Secret handling

Two of v1's seven fatal flaws were here.

**`sops -d` never reaches a shell environment** (flaw 3). `/proc/<pid>/environ` is readable,
`set -x` echoes, child processes inherit, argv is world-visible, and `trap` does not run on
SIGKILL. Decrypt to a `umask 077` **file** inside the staging directory, never `export`, and
`set +x` around the region.

**`<island>.conf` is parsed as DATA, never sourced** (flaw 4). `source` on the age-key host
is arbitrary code execution triggered by a fat-fingered config. Strict `KEY=VALUE` allowlist;
validate `REMOTE_PATH` and `SSH_ALIAS` against a pattern before use.

**Retention is capped** (flaw 6). `releases/` holds decrypted `.env` files — plaintext
secrets, one per generation. Keep N (propose 3) and delete the rest on each ship. An
unbounded release history is an unbounded secret history.

**The age key never leaves the control side.** Unchanged from v1 and affirmed by all three
families. SOPS is absent on both boxes and stays absent.

---

## 7. Staying clear of the recorded FATAL

A `/crucible` converged **FATAL** on reactive deploy: migrate to N+1, fail `/health`, roll
back the image, and old code runs against a forward-migrated schema. Tesla: *"backup without
restore-on-failure is a souvenir, not a spine."* Gated on the additive-only migration lint
(#3188 / #2615).

**This design ships CONFIG. It does not decide when to deploy, and the operator still runs
it.** But the hazard is sharper here than in design 15, because an atomic flip *feels* like
a transaction, and transactions invite automation.

**Constraints, stated as constraints:**

- The generation carries no scheduler, no watcher, no "check for a newer generation."
- `restore <ts>` is **config rollback only**. It must say so where an operator reads it,
  because the failure it will be reached for is often a migration failure, which it cannot
  fix.
- A generation whose `ISLAND_VERSION` differs from the running one is a **combined config +
  image change**, and that is where the FATAL lives. The design should treat that as a
  distinct, louder operation — flagged here as an open question for temper (§9).

---

## 8. The bootstrap boundary, and the honest limit

**`standup.sh` is irreducibly box-resident (#4749).** It creates the `aiko_data` volume,
writes the genesis `.env`, and pulls the image — so at the moment it runs, there is no
generation and no image. A generation cannot bootstrap an island. Affirmed unanimously at
design 15's temper; carry Tesla's caveat: this pins bootstrap **orchestration**, not ongoing
deploy **policy**.

**The handover:** `standup.sh` creates the island and the volume. The first `ship` adopts it —
reads the existing `.env`, writes generation 1 around it, flips `current`. From then on,
every box-resident file the repo owns arrives as cargo.

**The honest limit, and it is the one a temper should hit hardest:** this helps an operator
who uses the control-side path. A stranger running an island from the public repo must
either adopt the same tooling — possible; the repo is public and AGPL, and they bring their
own age key and secrets — or hand-sync, exactly as today.

So **ISL-0003's limit is closed for operators who ship generations, and unchanged for
operators who do not.** That is narrower than "the class is closed," and this design should
not be described as closing it. What it does remove is the *default* path being the drifting
one, which is what actually caused 2026-09-11: not an absent capability, but a procedure.

---

## 9. What temper should strike at

1. **§2's central claim** — does push-delivery really dissolve the bootstrap loop, or has it
   moved to "the operator's laptop is now a single point of failure with an age key on it"?
   This is the load-bearing claim and the one I am least able to attack myself, having just
   built the argument.
2. **§8's limit** — if the fix only helps operators who opt into the control-side path, is
   this ISL-0003's limit *closed* or merely *defaulted differently*? Does that justify the
   build?
3. **§7's combined change** — a generation that also bumps `ISLAND_VERSION` is a config +
   image + possibly migration change flipping atomically. Is that one operation or three,
   and does making it feel atomic make the FATAL *more* likely rather than less?
4. **§3's boundary** — is "every box-resident file the repo is authoritative for" a decidable
   set, or does it acquire exceptions until it is a curated list that drifts like the thing
   it replaced?
5. **Option 3, again, honestly.** `preflight-compose-drift.sh` exists and works. Is the
   marginal gain — `.env` coverage plus a synced `deploy/` — worth a new delivery mechanism,
   or is "one manual sync per box" simply the cheaper answer? Nick picked option 1 with this
   explicitly on the table, so this is not a re-litigation of the decision; it is the check
   that the *implementation* still earns it.

**Procedural note carried from v1's closing rule:** v1's recast was adversary-originated, so
v2's *shape* is not author-laundered — but this specific assembly has not been struck.
Before any build: a confirming cross-family strike on this document, **and** the standing
`/cage-match` on the built PR, since this is deploy and secret handling.
