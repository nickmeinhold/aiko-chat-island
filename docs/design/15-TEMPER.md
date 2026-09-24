# TEMPER.md — deploy tooling in the attested image (design 15 / #4684)

**Overall verdict: DISSOLVE — unanimous, 4/4 families.**
**Struck:** `dt-1790262504`, 2026-09-24. Families seated: Maxwell (Claude), Kelvin
(Gemini 2.5-pro), Carnot (Codex/GPT), Tesla (Grok). Wu/Kimi disabled. **Full panel — no
dark seat.**

Per `/design-temper`'s rule, DISSOLVE is decisive at ≥2 families. This was 4/4, including
the author instance. The candidate is invalidated: **Nick's 2026-09-21 option 2 was struck
at temper and does not proceed to a build.** The re-pick is his, because the strike lands
on the frame he chose — the same posture as the reactive-deploy temper.

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Maxwell (Claude) | DISSOLVE | Never asked why the box isn't a git checkout, though declarative-deploy names that fact as a *defect*; the dissolve chains and question 5 answers itself. |
| Kelvin (Gemini) | DISSOLVE | *"A high-quality document describing a solution that should not be built. The reasoning is sound, but the premise is slag."* |
| Carnot (GPT) | DISSOLVE | *"That is entropy added to reduce entropy."* The class was never 'too much shell'; it was 'the thing that must enforce drift is itself unsynced.' |
| Tesla (Grok) | DISSOLVE | The guard that actually failed cannot move into the image without trusting the accused or planting a toolchain on a sovereign box — and the 14% that can move is not that guard. |

## Fatal flaws (deduped, most-severe first)

1. **The dissolving alternative was already in the bundle, already designed, already
   part-shipped** — raised by **Kelvin, Carnot, Maxwell** (Tesla concurs from the
   control-side angle). `docs/crucible/47-declarative-deploy/DESIGN.md` v2 ships the
   complete cohort from the control side — *"the artifact in git IS the artifact on the
   box"* — step 1 landed in PR#166. It removes the unsynced surface rather than shrinking
   it, handles `.env` (which design 15 and a git checkout both structurally cannot), and
   advances or refuses **atomically**. Maxwell's git-checkout proposal is the same shape
   and strictly worse on both counts.
   **DISPOSITION: the successor work is completing declarative-deploy v2, not recasting
   this.**

2. **Increment 2 cannot be built — §4c and §5a are the same wire pulled to opposite
   grounds** — raised by **Tesla**, with Carnot independently on the trust-order half.
   §4c concedes the reorder needs the shim to verify attestation first. Verifying SLSA
   needs a sigstore/cosign-class verifier. §5a forbids exactly that: the shim is bash,
   because ISL-0003 says a toolchain is an attack surface on a box whose owner signed up
   to run a chat server. **There is no third state where attestation is verified and the
   box gains nothing.** *"An attestation nobody verifies is a label."* Present since 0.9.5
   is not checked.
   **DISPOSITION: the #4684 retraction is UPHELD. Do not ship §4b's reordering.**

3. **`docker pull` does mutate the host, so "before anything is pulled" was never only
   thrift** — raised by **Tesla**, and it directly refutes design 15 §4b. Layers land in
   the **shared** docker store on boxes also running dreamfinder, lyra and human tenants.
   §5d forbids prune and every box-wide op, so **every refused deploy is a permanent
   allocation.** Fill that disk and the island stops serving while the old container is
   still "up" — destroying the very safety property §4b claimed to preserve. Tag motion
   between pull and `docker run` is the same chord.
   **DISPOSITION: §4b is wrong on its own terms, not merely outweighed.**

4. **Increment 1 is NOT independent of §4, and saying it "should not wait" is a
   detonation timer** — raised by **Tesla**; Carnot reaches the same place via "the first
   increment proves only that a rewrite preserves known behaviour."
   - The 334 lines inherit the same three-way fork (running image = one release late;
     pulled image = self-certification; control side = no image boundary needed).
   - Running them from the pulled image is `docker run` of registry-supplied code, on
     enspyr **via `sudo -n docker`**, over the secret-bearing `.env` and credential files
     those functions must read to decide — **secret disclosure to an image whose
     attestation the shim does not verify**, one increment *before* the control meant to
     precede it.
   - §3e refuses the docker socket and then stops one metre short: you needn't hand the
     container the socket when the shim's privileged docker user executes the image's own
     entrypoint. **The shim contract never pins `--entrypoint`, `--network none`,
     `--read-only`, or a non-root user.** This image's entrypoint migrates `aiko_data` on
     start. A "predicate" that is really the entrypoint joins the mesh as a second gateway
     or migrates the volume — and **its non-zero exit is indistinguishable from REFUSED**,
     collapsing the three outcomes §5b exists to keep apart.

5. **`SHIM_CONTRACT` is prose functioning as executable governance — the exact thing the
   design quotes as its warrant** — raised by **Tesla, Carnot, Maxwell** independently.
   Two shims can both say `3` and mount different paths; the integer changes when someone
   *remembers* the invocation surface changed. The image can refuse an old shim only after
   that shim was hand-updated to ask, so every box — including the one that ate the
   outage — stays dark until the same manual `deploy/` sync ISL-0003 already confessed.
   **No channel in this design delivers shim updates**, and the image cannot be that
   channel without extracting executable bytes onto the host, which is root on the
   `sudo -n docker` box and is the automatic deploy §6 correctly forbids. *A detector that
   cannot move the thing it detects has relocated ISL-0003 to a smaller file.*

6. **"14%" is a unit error dressed as humility** — raised by **Tesla**, with Carnot's
   "success criterion is too local" and Kelvin's "shrinks the problem without eliminating
   the class" landing on the same target. The denominator is padded with `standup.sh`
   (781, one-shot bootstrap) and `verify-secrets.sh` (252, already control-side). **Line
   count is not the hazard. One forwarding decision is** — `APNS_VOIP_TOPIC`, one line of
   compose, several minutes of outage. Increment 1 states outright that nothing on the
   deploy's critical path changes behaviour: **a first ship that leaves the failure mode
   green has changed runtime on a neighbouring bench.**

7. **The premise may already be satisfied** — raised by **Maxwell**. ISL-0003's *"prose
   functioning as executable governance with no compiler"* was the design's warrant, but
   `preflight-compose-drift.sh` **is** that compiler, it exists, and it ran clean on both
   boxes on 2026-09-21. #4230 was caused by there being **no** guard, not by a guard in
   the wrong language. Design 15 silently upgrades "the guard lives somewhere that doesn't
   sync" into "the governance still has no compiler." The first is true and narrow; the
   second would justify the migration and is no longer true.

## What holds (unanimous — carry these forward)

- **The three-home frame (§1).** Control side / image / box have genuinely different
  binding constraints, and #4684's two-home question was the wrong cut. All four families
  affirmed it. Tesla adds the part design 15 saw and then failed to use: **the legal
  compiler already sits on the control side.**
- **`standup.sh`'s bootstrap constraint is total (§3b).** Volume genesis and first `.env`
  run before any image exists; no pre-seeded-image trick reaches the moment before the
  first pull. **#4684 did not know this — it names only the drift check.** Tesla's caveat
  matters: this pins bootstrap *orchestration* to the box, and must not be used as an
  argument for moving *policy* into the image. **Carry into ISL-0003 regardless of this
  DISSOLVE.**
- **The docker socket stays unmounted (§3e).** Root-equivalent authority on a shared
  multi-tenant host. Sound — though Tesla notes it is *not sufficient*, per flaw 4.
- **REFUSED / PASSED / COULD NOT RUN, fail closed, never warn-and-continue (§5b).**
  Carnot: *"exactly the predicate/lifecycle bug class this system has repeatedly hit."*
  The lesson holds wherever the check lives; the shim is the wrong place to spend it.
- **`verify-secrets.sh` is control-side (§3a)** and was miscounted by 252 lines.
- **§6's non-goal is load-bearing.** Image rollback is still not database rollback. Do not
  automate away the human who reads a one-line diff while closing a sync gap.
- **§7's split (§7).** Types protect decisions you construct, not values you parse; the
  measured bugs were in decisions. **But that argues for closed-set types and tests in CI
  — not for Dart on a sovereign box's deploy path.**
- **§4a.** Gating the pull of image N+1 with logic inside N+1 is circular. §4d's narrower
  gain is real and needs no policy move: **bind the digest**, keep the diff's logic
  outside the thing being judged.

## Disposition

**DISSOLVE at 4/4 — candidate invalidated. Do not re-cast this document.**

Tesla's fold-back list is one word — *"Nothing"* — and the panel's reasoning supports it:
folding design 15 down to "just the 334 lines" is precisely the shape that fails question 5
while opening a secret-to-unverified-image path and leaving the compose guard where it is.

**If a successor is written it is a different document, not a recast of this one.** The
shape the panel converged on, unprompted, from four directions:

- Finish **declarative-deploy v2**: control-side decrypt, ship a complete generation
  (compose + `update.sh` + mosquitto config + `.env`), flip `current` atomically, then run
  `update.sh`. That attacks the unsynced-artifact class instead of relocating helper logic.
- Keep refuse-and-show, but **on the control side**: read the box's compose, compare to the
  tag about to be pulled, print the diff, refuse before ship — without executing the image.
- Give the three pure decision scripts **closed-set types and tests in CI**, still as
  functions over files. That is the cheap version of §7's win and needs no new boundary.
- The box keeps backup → **pull by digest** → up → `/health`, with today's bash drift check
  until control-side ship is the only writer.
- The shim gains **no** contract integer, **no** attestation stack, and **no** entrypoint
  into the image.
- One manual `deploy/` sync remains the honest limit for an operator who never uses the
  control-side path — cheaper than a permanent second language, and already named in
  ISL-0003.

**Owed to Nick:** the re-pick. This strike invalidates the option he chose on 2026-09-21,
and the panel's alternative is work that already has a home (**#2301**; `#47` in older docs
does not resolve — see #4750), so the
choice is his to make rather than one to infer.
