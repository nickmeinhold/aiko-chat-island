# Declarative island deploy v2 — design (#2301)

Status: **round 2, folded from a 4/4 RECAST** (2026-09-24). Owed a re-strike before any code;
the built PR additionally owes a `/cage-match` — this is deploy and secret handling, which is
cage-match-by-law.

**Nick's pick, 2026-09-24 (#4750, option 1)**, after #4684 was DISSOLVED 4/4. Round 1 of this
document was itself RECAST 4/4 — verdict and the ten flaws in
**[16-TEMPER.md](16-TEMPER.md)**, raw strikes in `16-strikes/`. This is the fold.

> **TWO LAYERS ARE DELETED AND MUST NOT RETURN.**
>
> **From v1's temper (finding 7):** no `.env.template`, no `envsubst`, no render step, no
> missing-var map, no byte-match-as-proof gate. Store the **complete** encrypted `.env` per
> island. *"The artifact in git IS the artifact on the box."*
>
> **From round 1's temper:** no shim, no contract integer, no image entrypoint. Tesla:
> *"That corpse stays buried."*
>
> A templating layer or a box-resident vehicle appearing in a later revision is a fossil
> returning and should be struck on sight.

---

## 1. The problem, stated as a property

The island is two halves that update by different mechanisms. **The image updates itself** —
pinned, pulled, exact. **`docker-compose.yml`, `deploy/` and `.env` arrive by hand**, and
drift.

On 2026-09-11 that cost `chat.enspyr.co` several minutes: `APNS_VOIP_TOPIC` became required,
the box's `.env` had it, the box's **compose did not forward it**, and the island refused to
boot. One line of difference. imagineering had the identical gap and deployed clean because
its compose had been hand-copied more recently — **one variable: which files the operator
happened to be thinking about.**

ISL-0003 names the class: **prose functioning as executable governance with no compiler.**

**Already fixed; do not re-solve.** `preflight-compose-drift.sh` refuses on drift, before the
backup and before anything is pulled. It is a real compiler and it ran clean on both boxes on
2026-09-21. Option 3 on #4750 — *accept the limit* — was a genuinely close call for this
reason.

**What remains:** the guard lives in `deploy/`, the half that does not sync. **The detector
cannot move the thing it detects.**

> **ROUND 1 CARRIED A THESIS LINE HERE AND IT HAS BEEN DELETED.** It read: *"stop detecting
> drift, and remove the thing that drifts — a cohort that arrives complete has nothing to
> compare against itself."* Tesla: that sentence contradicts §5, which correctly **keeps** a
> refusal because local state is real, and *"is how an implementer rebuilds auto-sync."* The
> banner at the top of this file exists because a stale claim beside a live mechanism is how
> a deleted layer returns. **The thesis was that claim.** Drift detection stays. What changes
> is that the repo-authoritative files arrive as a cohort instead of as a manual prelude.

---

## 2. What push does and does not dissolve

**What it dissolves, and this is real.** Design 15 died on a bootstrap loop: box-resident code
had to *fetch* the code that replaced it. A push has no such loop — the control side holds the
repo, the key and ssh, and can lay `deploy/` down as **cargo** rather than needing it as the
**vehicle**. That asymmetry is why option 1 was reachable and option 2 was not.

**What it does not dissolve — stated here because round 1 claimed otherwise and §8 contradicted
it in plain words.**

- **§5 will not place a generation until the box answers.** `current` must be readable and the
  island identified; COULD NOT RUN fails closed. *A dangling `current` is a repair that
  requires the artifact whose corruption is the failure.*
- **§6 will not place one without the age key**, and because `.env` is inside the cohort,
  **a one-line public compose forward — the exact 2026-09-11 repair — cannot move unless that
  key is alive.** Key loss blocks every config change, not merely secret rotation.
- **The control side is itself unsynced.** An operator running the shipper from a stale
  checkout produces a malformed generation. The drift moved from the box's `deploy/` to the
  operator's clone.
- **ISL-0003's limit is unchanged for anyone not using this path.**

**The true claim, in words that cannot be quoted as "the loop is gone":**

> The box-resident fetcher loop is dead. The control side is a single point of **availability**
> and of **compromise** for every island it ships. ISL-0003's limit is closed only for
> operators who walk this path.

**Mitigations, required rather than optional:**

- **A second age recipient**, so one laptop is not the only decrypt path. Without it, a lost
  key is a permanently unmaintainable island.
- **Break-glass is specified as *placing a generation and swinging `current`*** — never as
  editing live files. A hand-edit in the old tree is drift the next ship refuses, so recovery
  must re-enter the cohort or it is not recovery.
- **The shipper pins its own provenance.** It refuses to run from a checkout whose HEAD is not
  an ancestor of the remote's default branch, and stamps its own git sha into `GENERATION.txt`.
  *(Kelvin proposed `curl … | bash` instead. **Rejected** — that is remote code execution on
  the operator's machine, ISL-0003's founding objection to push-CD. The finding was right and
  the fix was not.)*

---

## 3. What a generation is — a closed manifest, not a quantifier

Round 1 said *"every box-resident file the repo is authoritative for"* and *"ALL of
`deploy/`"*. Tesla: a universal wearing an enumeration, with an open exclusion no tool can
evaluate — and it **shipped every island's ciphertext to every island.**

**The shipped set is a denylist-closed manifest, asserted in CI:**

```
INCLUDE  docker-compose.yml
INCLUDE  mosquitto.conf
INCLUDE  deploy/**
  DENY   deploy/secrets/**        # every island's ciphertext; ships nothing
  DENY   deploy/islands/**        # control-side manifests
  DENY   deploy/verify-secrets.sh # control-side: guards repo artifacts, not box state
  DENY   <the shipper itself>     # control-side vehicle, never cargo
DECIDED: caddy/ is OUT — it belongs to the reverse-proxy stack, not the island.
         (v1 carried it; round 1 silently dropped it. Now stated either way.)
```

**The denylist is the closed set.** "Legitimately local" is not a membership rule and must not
appear as one.

```
<REMOTE_PATH>/
  releases/
    20260924-233000/
      docker-compose.yml
      deploy/                  # per the manifest above
      mosquitto.conf
      .env                     # decrypted from deploy/secrets/<island>.env.sops
      GENERATION.txt
  current -> releases/20260924-233000     # swung with `mv -T`
  backups/                     # OUTSIDE every generation — see 3b
  local/                       # OUTSIDE every generation — see 3c
```

**Never in a generation:** `aiko_data` (external named volume), `backups/`, `local/`.

### 3a. `.env` is why this earns its keep

`preflight-compose-drift.sh` **cannot reach `.env` by construction** — per-box secrets versus a
public tag. It is the one file no existing guard can see and it is half the config surface.
Shipping the cohort covers it because the control side holds the key. **That capability, plus
`deploy/` arriving as cargo instead of as a manual prelude, is the entire marginal gain over
option 3.** Narrower than "the class is closed." Worth doing.

### 3b. `backups/` is outside the generation, and CI proves it

**Round 1's worst defect.** `update.sh` is invoked from `current/deploy` — that is,
`releases/<ts>/deploy` — so an unchanged script resolves its *relative* backup path **into the
generation**, which retention then prunes. **The only undo for the recorded FATAL would be
stored in the thing that rotates.**

- Backups resolve to `<REMOTE_PATH>/backups`, absolute, never relative to the generation.
- **A CI test asserts it**: with cwd at `releases/<ts>/deploy`, the resolved backup path is
  `<REMOTE_PATH>/backups` and is not under `releases/`. This is a gate, not a note.
- **Prune never unlinks** the target of `current`, nor the generation whose digest is running.
- **Prune shreds `.env`** before removing a generation — plaintext secrets, one per generation.

### 3c. `local/` is where box-local state lives

Round 1 quoted v1's *"a box may legitimately carry local state"* and then removed the state's
home: an operator's 3am edit to `current/.env` is not clobbered, it is **orphaned** — the file
still exists and still says what they wrote.

`local/` sits outside `releases/` and survives every flip. A generation records a manifest hash
of what it placed; if `current/` has been modified since placement, the ship is **REFUSED**,
turning an orphaned edit into a caught one.

---

## 4. Two ships and one restore — the split that closes the FATAL

Round 1 had one command and claimed *"before it, nothing is live; after it, everything is."*
That is v1's overclaim relocated from `mv` into the word **live**: `rename(2)` swings one
directory entry, and containers, the pulled image and `aiko_data` do not swing with it.

**The honest statement, which replaces it:**

> The swing atomically changes **which files `current` names**. Running containers change only
> after `up` returns healthy. There are **two facts on disk** — *intended* and
> *running-as-of-last-green* — and incident response must be told which one it is reading.

### 4a. The three commands

| Command | May change the pin? | Pulls | Refuses when |
|---|---|---|---|
| **`ship-config`** | **no** | never | `ISLAND_VERSION` or image digest differs from what is running |
| **`ship-release`** | **yes** — the only path that may | **by resolved digest, never by tag** | no bare `--yes` accepted |
| **`restore <ts>`** | **no** | never | the generation left and entered name **different digests** |

`restore` across a digest boundary **refuses and prints the absolute path of the sqlite
backup.** There is **no image rollback in the tool.** Round 1 left this as an open question;
leaving the trigger armed and naming it is not staying clear of the FATAL.

Why the split does the work: `ISLAND_VERSION` lives in the cohort's `.env`, so under one
command a compose fix and a migration become **one gesture** whenever the encrypted env has
been bumped. Separating them means the ordinary operation — the 2026-09-11 repair — **cannot**
carry a migration.

### 4b. Bind the digest

`GENERATION.txt` records an image digest while inherited `update.sh` pulls a mutable tag
(`ISLAND_VERSION`; ISL-0003's `edge`/`latest` exist). The file would be wrong twice: once
because the flip precedes `up`, and again because the tag can move between resolve and pull.
**`ship-release` resolves the digest control-side and pulls that digest.** This was design 15's
one surviving technical gain and it needs no policy to move.

### 4c. Pin the syscall: `mv -T`

A plain `mv` onto an existing symlink-to-directory **nests the new generation inside the old
one** and the one-inode swap never happens. Staging stays **mandatorily under `REMOTE_PATH`**,
never `/tmp` — `/tmp` turns `mv` into copy+unlink and reopens the truncation class that caused
the original incident. Fail if `releases/<ts>/` already exists.

---

## 5. Baseline, target, live — the predicate must name the state it proves

Round 1 diffed the box against the generation being shipped and refused on difference. Carnot:
that makes **a legitimate config update indistinguishable from unauthorised drift** — a
specification bug, not an implementation detail.

**Three states, never two:**

| | |
|---|---|
| **baseline** | the generation the operator believes the box is running |
| **target** | the generation being shipped |
| **live** | what ssh actually reads from `current` |

- **REFUSE when `live != baseline`.** That, and only that, is drift.
- **SHOW `target - baseline`** as the intentional change for human review. **Do not call it
  drift.**

ISL-0003's refuse-and-show posture survives verbatim: refuse and show, never auto-sync. A box
may legitimately carry local state (§3c), and silently clobbering it is how #2301 became
standing instead of caught.

### 5a. Two privilege classes, two acknowledgements

Refuse-and-show worked in 2026-09-11 because the diff was **one forwarding line a person could
read**. A cohort of all `deploy/` plus compose plus `.env` saturates that: *the operator
rubber-stamps a wall, and the line that matters is the one they did not see.*

And the classes are not equivalent. **Hunks under `deploy/**` are not config** — on enspyr they
execute as `sudo -n docker`, daemon-root, the moment the control side invokes the new
`update.sh`.

**The diff is presented as per-file hunks, and executable hunks under `deploy/**` require a
separate acknowledgement from config hunks.** *The human is the compiler only if the two
privilege classes are not one yes.*

### 5b. Three outcomes, fail closed

| Outcome | Meaning | Exit |
|---|---|---|
| **REFUSED** | compared, and said no | non-zero, diff on stderr, box untouched |
| **PASSED** | compared, and affirmed | zero |
| **COULD NOT RUN** | ssh failed, `current` missing/unreadable/dangling, island unidentified | **non-zero** |

A check that could not run is not a check that passed.

### 5c. Ask the daemon, not the letter the shipper wrote itself

Round 1 identified the island from `current/GENERATION.txt` — **authored by the control side**.
A wrong first write poisons every later preflight: the check confirms the box matches the lie.

Worse, round 1 treated **absence of `current` as adoption**. Docker's identity is project
`aiko` plus external volume `aiko_data`, **constant across directories on a shared host** — so
an empty wrong path gets adopted, `compose up` runs, and **it attaches to the real tenant's
volume from a decoy directory.** Split brain, with the drift detector pointed at the decoy.

- **Absence of `current` is never genesis.** Genesis is `standup.sh`, once. Deletion, a
  dangling symlink, and "please adopt" are three different states.
- **Adoption is an explicit `--adopt` flag**, never inferred.
- **On every ship**, read live `.env` `DOMAIN` **and ask docker which compose-file path project
  `aiko` was last started from**; refuse unless that path resolves under this `REMOTE_PATH`, or
  the project does not exist and `--adopt` was passed.

---

## 6. Secrets

Carried from v1's temper, plus what round 1 forgot.

- **`sops -d` never reaches a shell environment.** `/proc/<pid>/environ` is readable, `set -x`
  echoes, children inherit, argv is visible, `trap` does not run on SIGKILL. Decrypt to a
  `umask 077` **file**; never `export`; `set +x` around the region.
- **`<island>.conf` is parsed as DATA, never sourced.** `source` on the age-key host is
  arbitrary code execution from a fat-fingered config. Strict `KEY=VALUE` allowlist; validate
  `REMOTE_PATH` and `SSH_ALIAS` against a pattern.
- **Shred the laptop's temp plaintext on every exit**, including the local half of the copy.
  Round 1 specified the box and forgot the pocket.
- **Modes at creation, not chmod-after:** generation directories `0700`, `.env` `0600`.
- **Capped retention** with shred-before-unlink (§3b).
- **A second age recipient** (§2), so the key is not a single point of failure.

---

## 7. The recorded FATAL

Image rollback is not database rollback: migrate to N+1, fail `/health`, roll back the image,
and old code runs against a forward-migrated schema. Tesla, in the original crucible:
*"backup without restore-on-failure is a souvenir, not a spine."* Gated on the additive-only
migration lint (#3188 / #2615).

**Round 1 named it and left it armed.** The fold disarms it mechanically rather than in prose,
because *"it must say so where an operator reads it"* **is** prose functioning as executable
governance — the class this design quotes as its warrant:

- `ship-config` cannot change the pin, so the ordinary operation cannot carry a migration.
- `ship-release` is the only path that can, pulls by digest, and takes no bare `--yes`.
- `restore` refuses across a digest boundary and prints the backup path.
- **No scheduler anywhere.** Round 1 forbade one *inside the generation*, which does not forbid
  one on the control side — now the natural home for a Friday cron, with inherited `--yes` as
  the accelerator. **The shipper is invoked by a human. It never initiates.**

---

## 8. Bootstrap, cutover, and the honest limit

**`standup.sh` is irreducibly box-resident (#4749).** It creates `aiko_data`, writes the
genesis `.env`, and pulls the image — at that moment there is no generation and no image. This
pins bootstrap **orchestration**, not ongoing deploy **policy**.

**Cutover, restored from v1's temper and missing in round 1.** Round 1's *"writes generation 1
around"* the live `.env` either consecrates box drift as the new authority or clobbers
production secrets — in one gesture, with no review. v1's finding 1 killed
byte-match-**as-proof**; it did not kill the **direct diff of the real object**:

> Before the first flip, `diff` the decrypted sops bytes against the live `.env` and **REFUSE
> on mismatch.** Do not consecrate and do not clobber. The operator reconciles, then adopts.

**The honest limit.** This helps an operator who uses the control-side path. A stranger either
adopts the same tooling — possible; public repo, their own age key — or hand-syncs as today. So
**ISL-0003's limit is closed for operators who ship generations and unchanged for those who do
not.** What it removes is the *default* path being the drifting one, which is what actually
caused 2026-09-11: not an absent capability, a procedure.

---

## 9. What the re-strike should hit

1. **§2's mitigations** — does a second age recipient plus a provenance-pinned shipper actually
   discharge the single-point-of-compromise finding, or only the single-point-of-availability
   half? Compromise of the control side now reaches both islands' config.
2. **§3's denylist** — is it closed, or will it acquire exceptions until it is the curated
   hand-list this design exists to retire? The `caddy/` decision is stated; what is the rule
   that decides the *next* ambiguous directory?
3. **§4's split** — does `ship-config` refusing on a digest difference make the common case
   *harder*, such that operators reach for `ship-release` routinely and the separation
   evaporates in practice?
4. **§5a's two acknowledgements** — is a second prompt a real gate or a second reflex? What
   makes an operator actually read the executable hunks?
5. **§3c's `local/`** — does a composed-over local directory reintroduce, at a different layer,
   the two-sources-of-truth problem the cohort exists to remove?
6. **Option 3, once more on implementation grounds.** After this fold the mechanism is larger
   than round 1's. Carnot's economy test: *if the implementation exceeds a small control-side
   shipper plus restore, fall back to one manual `deploy/` sync per box plus the existing
   preflight.* Does the folded design still pass its own test?
