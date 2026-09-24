# Deploy tooling in the attested image — design (#4684)

> ## ⚠️ DISSOLVED AT TEMPER — 4/4 FAMILIES, 2026-09-24. DO NOT BUILD FROM THIS FILE.
>
> `/design-temper` run `dt-1790262504`, full panel (Maxwell, Kelvin, Carnot, Tesla), no dark
> seat, **unanimous DISSOLVE**. Nick's 2026-09-21 option 2 was struck and does not proceed
> to a build. Verdict and the seven deduped fatal flaws: **[15-TEMPER.md](15-TEMPER.md)**.
>
> The headline: the dissolving alternative was already in the bundle. Declarative-deploy v2
> ships the complete cohort from the control side and *removes* the unsynced surface rather
> than shrinking it. Carnot's framing — **"the class was not 'too much shell'; it was 'the
> thing that must enforce drift is itself unsynced'"**.
>
> Specific claims below that are now REFUTED, not merely outweighed:
> - **§4b** — "before anything is pulled is thrift, not safety" is **wrong on its own terms**.
>   `docker pull` mutates a shared multi-tenant host; §5d forbids prune; every refused deploy
>   is a permanent allocation, and a full disk stops the island while the old container reads
>   as "up". The #4684 retraction is **upheld**.
> - **§4c / §5a** — mutually exclusive. Verifying SLSA needs a cosign-class verifier; §5a
>   forbids a toolchain on the box. Increment 2 cannot be built.
> - **§8** — Increment 1 is **not** independent of §4. Those functions read secret-bearing
>   files, and the shim pins no `--entrypoint`, so a container's non-zero exit is
>   indistinguishable from REFUSED — collapsing §5b's own three outcomes.
> - **§5c** — `SHIM_CONTRACT` is prose functioning as executable governance, the exact thing
>   this design quotes as its warrant. No channel here delivers shim updates.
> - **§3f** — "14%" is a unit error: the denominator is padded with bootstrap and
>   control-side lines. Line count is not the hazard; one forwarding decision is.
>
> **What survived, unanimously**, and should outlive this document: the three-home frame
> (§1); `standup.sh`'s bootstrap constraint being total (§3b — #4684 does not know this, and
> it belongs in ISL-0003 regardless); the docker-socket rejection (§3e); REFUSED / PASSED /
> COULD NOT RUN, fail closed (§5b); `verify-secrets.sh` being control-side (§3a); §6's
> non-goal; and §7's decisions-not-parsers split — which argues for **closed-set types and
> tests in CI**, not for a language boundary on a sovereign box's deploy path.
>
> Kept for the reasoning and the rejected alternatives. **The re-pick is Nick's** — the
> strike lands on the frame he chose, same posture as the reactive-deploy temper.

Status: **DISSOLVED at temper 2026-09-24** (was: design, not build-ready; the temper it was
owed is done and is what dissolved it). Nick's call 2026-09-21: option 2 — move this repo's deploy tooling into the
already-attested image, leave a thin shim on the box. Scoped to **this repo's scripts
only**; cage-match's own Dart rewrite lives in `claude-skills` and is that session's
work.

The argument for doing this at all is Nick's own, from ISL-0003's amendment written
after one line of compose drift took `chat.enspyr.co` down:

> **prose functioning as executable governance with no compiler.**

---

## 1. The frame is wrong in the issue, and fixing it changes the answer

#4684 asks what moves **into the image** versus what **stays on the box**. That is two
homes. There are three, and the third is already load-bearing.

`docs/crucible/47-declarative-deploy/DESIGN.md` (v2, shipped in PR#166) establishes
**control-side render, box-side execute**: SOPS and the age key live on the laptop,
never on a box, and `deploy/secrets/<island>.env.sops` holds the complete `.env` —
*"the artifact in git IS the artifact on the box."*

So every script has three candidate homes, each with a different binding constraint:

| Home | What it can assume | What forbids code here |
|---|---|---|
| **Control side** (laptop / CI) | full toolchain, SOPS, age key, network | cannot touch a box the operator hasn't invited |
| **The attested image** | its own runtime, SLSA provenance, syncs with the pull | cannot exist on the box before the first pull |
| **The box** | docker, bash, `.env` | *never syncs* — it is the thing that drifts |

Read that way, the question stops being "what moves" and becomes:

> **Which home does each script's binding constraint actually force it into, and how
> small can the un-syncing surface be made?**

That reframe is the design's main contribution. It also produces a different answer
than the issue anticipated, in §3.

---

## 2. The real goal: shrink the un-syncing surface, and make its drift detectable

ISL-0003 records this honest limit on the drift guard:

> The guard lives in `deploy/`, which is the thing that does not sync — so a box whose
> `update.sh` predates it will not run it, and protection starts on the deploy *after*
> the operator syncs `deploy/` once by hand.

Moving tooling into the image is a fix for exactly that. But **the shim has the same
property.** Whatever remains on the box still does not sync, so this design cannot
eliminate the class — only shrink it.

That gives the real success criterion, and it is a measurable one:

1. **Minimise the box-resident surface** — fewer lines, fewer files, and above all
   fewer *decisions* living where nothing updates them.
2. **Make the remaining surface's drift detectable from the syncing side** — the image
   should be able to say "the shim on this box is older than I expect," because a
   surface that cannot report its own version reproduces ISL-0003's limit at a smaller
   size rather than closing it.

A design that moves 2,000 lines into the image and leaves a 300-line shim nobody can
version-check has relocated the problem, not solved it.

---

## 3. What moves, what cannot, and why — all seven scripts

`deploy/` is 2,373 lines across 7 top-level scripts (plus `lib/dotenv-read.sh`, 161).

### 3a. Does not belong in the image — it is already control-side

**`verify-secrets.sh` (252).** Reads SOPS *cleartext metadata*, needs no key, runs in
CI on every push. It guards **the repo's committed artifacts**, not a box's state. Its
own header says why it exists: *"Entropy wins when checks live in memory."* Putting it
in the image would move a repo-integrity check into a runtime artifact that the repo
produces — a circularity with no payoff, since the box has no `.sops` files to verify.

**Verdict: stays control-side. Out of scope for #4684.** Counting it in the 2,373 makes
the migration look larger than it is.

### 3b. Structurally cannot move — and it is not the one the issue names

**`standup.sh` (781).** This is the hard case, and #4684 does not mention it.

`standup.sh` stands up a brand-new island on a fresh host: it creates the external
`aiko_data` volume, writes the first `.env` with a freshly generated JWT secret, and
**pulls the image**. It is what puts the image on the box in the first place. No
ordering trick reaches it: at the moment it runs, there is no image to run tooling
from.

This is a stronger "cannot move" than the drift check's, because it is a genuine
chicken-and-egg rather than a sequencing constraint. **781 lines — a third of the whole
surface — is pinned to the box by bootstrap, and it is the largest single script.**

That finding materially shrinks what this project can achieve, and it should be stated
before anyone estimates the work.

*Partial mitigation worth designing:* `standup.sh`'s **decision** logic can still move
(see 3c) even though its **orchestration** cannot. A thinner standup that pulls the
image and then delegates its choices to tooling inside it is possible for every
decision made *after* the pull. The pre-pull portion — volume creation, `.env`
genesis — is irreducibly box-side.

### 3c. Clean candidates — pure decisions, already isolated, already tested

**`resolve-gateway-env.sh` (124)**, **`resolve-media-env.sh` (110)**,
**`preflight-apns.sh` (100)**. 334 lines.

These are the best candidates and the argument is unusually strong, because *this repo
already extracted them for exactly this reason*. From `resolve-media-env.sh`'s header:

> Every finding across three cage-match rounds on PR#151 was in `standup.sh`'s media
> wiring, and `standup.sh` cannot be tested: it needs a live Docker daemon and has no
> dry-run. So four fixes shipped with nothing guarding them, and round 1's bug came
> back inside round 3's fix precisely because nothing could catch it.

All three are already the right shape: take file paths, make a decision, exit non-zero
on refusal, emit no secret. They are pure functions wearing shell. Moving them into the
image is a change of *runtime*, not of *architecture* — which is why they should go
first and alone, as a testable increment that proves the shim contract before anything
risky depends on it.

### 3d. The contested one

**`preflight-compose-drift.sh` (654).** Treated in §4, because it deserves more than a
row in a table and because I disagree with the record about it.

### 3e. The orchestrator

**`update.sh` (352).** Needs the docker socket: it pulls, recreates, and verifies
`/health`. Running it *inside* a container means mounting `/var/run/docker.sock`, and
both live boxes are **shared and multi-tenant** (imagineering hosts dreamfinder/lyra;
enspyr hosts real human tenants). The declarative-deploy design's constraint is
explicit: *"tool must be a good tenant: no box-wide ops, per-app scope only."*

A container with the docker socket mounted is root-equivalent on the host and is by
construction **not** per-app scope. That is a sovereignty regression dressed as a
refactor, and it should be rejected on those grounds rather than debated on
convenience.

**Verdict: `update.sh`'s orchestration stays on the box as the shim's spine.** Its
*decisions* — is this `.env` partial, does this compose match, which credential file is
live — move into the image and are consulted.

### 3f. Summary

| Script | Lines | Home | Forced by |
|---|---:|---|---|
| `verify-secrets.sh` | 252 | control side | guards repo artifacts, not box state |
| `standup.sh` | 781 | **box (bootstrap)** | runs before any image exists |
| `update.sh` | 352 | **box (shim spine)** | docker socket ≠ good tenant |
| `preflight-compose-drift.sh` | 654 | **contested — §4** | ordering vs the pull |
| `resolve-gateway-env.sh` | 124 | **image** | pure decision, already isolated |
| `resolve-media-env.sh` | 110 | **image** | pure decision, already isolated |
| `preflight-apns.sh` | 100 | **image** | pure predicate over a file |

**Uncontested movement: 334 of 2,373 lines — 14%.** With the drift check, 988 — 42%.
Neither number is close to "move `deploy/` into the image", and the design should say
so plainly rather than let the issue's framing imply otherwise.

---

## 4. The drift check: where I disagree with the record, and how far

#4684 records a retraction, and it is unambiguous:

> `preflight-compose-drift.sh` CANNOT move — it must run BEFORE anything is pulled,
> while the island is still serving. Logic in the new image cannot gate the pull of the
> new image. The most it could do is run from the CURRENTLY RUNNING image, which
> relocates the old-code-gates-new-deploy property rather than removing it. My earlier
> "it dissolves" claim was wrong.

I arrived at the retracted position independently before reading the issue, which is a
reason for suspicion, not confidence — a fluent idea that a previous instance already
tried and withdrew is the textbook shape of a cached pattern. So this section states the
narrow version that I think survives, and flags it for temper rather than adopting it.

### 4a. The retraction's argument is sound about the thing it names

Gating *the pull of image N+1* with *logic inside image N+1* is circular. That part is
simply right.

### 4b. But the retraction considers two options where there are three

It weighs "logic in the new image" against "logic in the currently running image." There
is a third: **logic in the newly pulled image, which is not yet running as a service.**

`docker pull` is non-destructive. It writes layers to local storage and does not touch a
running container. A one-off `docker run --rm <new-image> <check>` executes new code
without the island having been recreated, while the old container is still serving.

Read ISL-0003's actual guarantee again:

> Any difference aborts **before the backup and before anything is pulled**, with the
> diff on stderr, while the island is still serving.

Two clauses, doing different work. **"Before the backup" is the safety property** — the
backup is the first state change, and refusing after it means the operator's box has
already moved. **"Before anything is pulled" is thrift**, not safety: it saves bandwidth
and disk on a box that was never going to deploy. Valuable, not load-bearing.

So the sequence *pull → check from the pulled image → refuse before backup and before
recreate* preserves the safety property and spends the thrift. That is not
old-code-gates-new-deploy. It is new code gating its own deploy.

### 4c. Is self-certification a real objection?

Partly. A broken or hostile image would be certifying its own deployment. But note what
the drift check is *for*: it catches **operator drift** — the box's compose having
diverged from the tag — not a malicious image. And the operator is about to run that
image as their island. If the image is not trusted, a refusing drift check is not the
control that saves them; **attestation is**, and it is upstream of both. Every image
since 0.9.5 carries SLSA provenance on the index digest.

The honest residue: this couples two controls that are currently independent. Today a
compromised image cannot suppress the drift check because the check runs from the box.
After this change it could. Whether that matters depends on whether attestation is
*verified* at deploy time or merely *present* — and **today the shim does not verify
it**. That is a real gap this design would create, and the fix (verify the attestation
in the shim, before running anything from the image) belongs in the same increment or
the change should not ship.

### 4d. The gain the retraction does not weigh

Today the check fetches the tag's bytes **from codeload over HTTPS at check time** and
compares them to the box. If those bytes ship *inside* the image, the comparison is
against **the exact artifact being deployed** rather than against a tag name resolved
separately. That removes a network dependency from the deploy path and closes a gap
between "the tag I compared" and "the image I pulled" that is currently held together
by both naming the same version string.

### 4e. Disposition

**Not resolved here.** The record and my reasoning disagree, and per CLAUDE.md that is a
finding to surface rather than tie-break. Temper should strike specifically at 4c — the
coupling of drift-refusal to image trust — because that is where I am least confident
and where a cheap-sounding reordering could quietly spend a security property.

If temper upholds the retraction, §3's uncontested 334 lines still stand on their own
and the design ships smaller. **This question does not block the first increment**, and
the increment should not wait for it.

---

## 5. The shim contract

The shim is the **only** operator-facing surface, and it is the thing that does not
sync. Its interface must therefore be the most stable element in the system — more
stable than the image's, because the image can change under a fixed shim but not the
reverse.

### 5a. The shim is bash. That is the sovereignty answer.

ISL-0003: *"a toolchain is an attack surface and a maintenance burden on a box whose
owner signed up to run a chat server."* There is no Dart on enspyr and there must not
be. **Dart goes in the image, which already carries its own runtime; the box gains
nothing new.** The shim uses what is already there: `bash`, `docker`.

### 5b. Three outcomes, never two

This repo's recurring failure is a check that cannot distinguish *passed* from *did not
run*. ISL-0003's own limit is an instance: a box whose `update.sh` predates the drift
guard simply does not run it, and warns. The shim must make that state **loud and
distinct**:

| Outcome | Meaning | Exit |
|---|---|---|
| **REFUSED** | the tool ran and said no | non-zero, diff on stderr, island untouched |
| **PASSED** | the tool ran and affirmed | zero |
| **COULD NOT RUN** | image missing, tool absent, attestation unverified | **non-zero**, named explicitly |

The third must **fail closed**, not warn-and-continue. A tool that cannot run is not a
tool that found nothing — that is this project's own predicate/lifecycle lesson, and the
current warn-and-continue arm is the same defect the design exists to remove.

### 5c. Version reporting is the whole point

Per §2, the shim must be able to report its own version and the image must be able to
refuse a shim older than it expects. Without this, the design shrinks the un-syncing
surface without making it detectable and reproduces ISL-0003's limit at 300 lines
instead of 2,373.

**Proposed:** the shim carries a `SHIM_CONTRACT` integer; the image declares the minimum
it accepts; a mismatch is COULD NOT RUN with a message naming the sync command. The
number changes only when the *invocation surface* changes — not when logic inside the
image changes, which is the point of moving logic there.

### 5d. Good tenant

Per-app scope only. No box-wide docker operations, no pruning, no touching containers
outside the compose project (`aiko`). Both live boxes are shared.

---

## 6. Staying clear of the recorded FATAL

A `/crucible` converged **FATAL** on reactive deploy: image rollback is not database
rollback. Migrate to N+1, fail `/health`, roll back the image, and old code runs against
a forward-migrated schema. Tesla: *"backup without restore-on-failure is a souvenir, not
a spine."* Gated on the additive-only migration lint (#3188 / #2615).

**Explicit non-goal: this design does not make deploy automatic.** It changes *where
deploy logic lives*, not *who decides when it runs*. The operator still runs the shim.

The hazard is drift toward it, and it is a real temptation rather than a theoretical
one: once tooling lives in the image, "the image could check for its own successor"
is one small step, and that step is the FATAL. **Stated as a constraint on the shim
contract: the shim never initiates. It is invoked by a human or the operator's own
scheduler, and the image is never given the means to trigger a deploy.**

---

## 7. The honest limit on the language, and the evidence that qualifies it

#4684 carries the correct caution:

> types protect values you CONSTRUCT and do nothing for values you PARSE. The win is
> orchestration, not parsing. Do not let the design smuggle in a parser rewrite as
> though the language fixes it.

Taken seriously: `dotenv-read.sh`, the compose walk, and the SOPS metadata reader are
all **parsers**, and Dart makes them no safer. A rewrite that claims otherwise is
selling the language.

**But the measured defect history of these specific scripts qualifies the caution in the
design's favour, and this is evidence rather than assertion.** From
`resolve-media-env.sh`'s own header:

> **the bugs were all in the decision, never in the read**, so the decision is what
> needs a test.

Three cage-match rounds on PR#151, every finding in the media wiring's *decision* logic.
`resolve-gateway-env.sh` exists because the same class was found again — an operator
*choice* silently reverting to a default, twice more (`ISLAND_SEED_PEERS`,
`PASSKEY_ENABLED`), both failing silently and looking healthy.

Choice-versus-default, source selection, all-or-none credential grouping: these are
**closed sets and sum types**, and they are exactly what this repo spent v0.15.0
learning to make unrepresentable rather than merely tested. A `SenderKind`-shaped fix
applied to `CredentialSource { sfu, gateway, none }` is the same move.

So: **the win is real and it is in the decisions, which is where this codebase's
measured defects actually are.** The parsing surface is unchanged and the design must
not claim otherwise — but "orchestration only" undersells it, because the recorded bugs
were not in orchestration either.

---

## 8. Proposed increments

**Increment 1 — prove the contract, move nothing risky.** Move the three pure decision
scripts (§3c, 334 lines) into the image as Dart with closed-set types for the decisions.
Build the shim with all three outcomes (§5b) and the version handshake (§5c). Nothing
in the deploy's critical path changes behaviour; the shim's contract is exercised by the
least dangerous callers available. **This increment is independent of §4 and should not
wait for it.**

**Increment 2 — the drift check**, only if temper resolves §4, and only bundled with
attestation verification in the shim (§4c).

**Increment 3 — thin `standup.sh`'s post-pull decisions**, accepting that its pre-pull
portion never moves (§3b).

`update.sh`'s orchestration and `verify-secrets.sh` are **not** in scope at any
increment, for the reasons in §3a and §3e.

---

## 9. What temper should strike at

1. **§4c** — does moving the drift check into the image spend a security property that
   attestation does not currently replace? Least confident claim in the document.
2. **§3b** — is `standup.sh`'s bootstrap constraint as total as claimed, or is there a
   pre-seeded-image path that dissolves it?
3. **§3e** — is "docker socket ≠ good tenant" decisive, or is there a scoped-socket
   arrangement that preserves per-app scope?
4. **§2** — is "shrink and make detectable" the right success criterion, or does any
   un-syncing surface at all mean the class is simply not closed by this design?
5. **The whole premise** — 334 uncontested lines out of 2,373 is 14%. Is that worth a
   new language boundary on a sovereign box's deploy path at all, or is the honest
   answer that ISL-0003's limit wants a different fix entirely?

Question 5 is the one that could DISSOLVE this, and it should be asked first.
