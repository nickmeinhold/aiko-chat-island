## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The subtraction is real and most of it holds, but the removal it all hangs on — taking `ISLAND_VERSION` out of the cohort — silently unpins every island to `edge`, and I measured that rather than reasoned it.

`Quint: "You go in the cage? Cage goes in the water. Shark's in the water."` I wrote this fold ninety minutes ago and the load-bearing beam is hollow.

**Is the subtraction real?:** **REAL.** Three commands → one, `deploy/**` → three names, two acknowledgements → one, digest-gated restore → plain restore, `local/` → gone. 284 lines deleted against 226 added, and the mechanism count fell further than the line count. Five of round 2's eight flaws are discharged by deletion — R2-2 and R2-6 in particular no longer have a *surface*, which is the difference between a fix and a dissolution. This is not cosmetic. It is also not sufficient, because of the first flaw below.

**Round 2 findings:**
- **R2-5 (`local/` is the bypass) — DISCHARGED.** Deleted outright. One mechanism, hash excluded from the bytes it hashes, CI greps for outside references. Nothing to police.
- **R2-7 (break-glass needs the lost key) — DISCHARGED IN FORM, BROKEN IN FACT.** A compose-only generation does ship keyless. See new flaw 2 for what it ships *into*.
- **R2-4 (split re-couples via `ISLAND_VERSION`) — NOT DISCHARGED.** The fix does not work. See flaw 1.

**New or surviving fatal flaws:**

- **`ISLAND_VERSION` CANNOT LEAVE `.env`, AND AS WRITTEN THE REMOVAL UNPINS BOTH ISLANDS TO `edge`. MEASURED.** `docker-compose.yml:48,329,344` read `image: ghcr.io/nickmeinhold/aiko-chat-island:${ISLAND_VERSION:-edge}`. That is **Compose interpolation**, which resolves from the shell environment or **the `.env` file in the project directory** — and `update.sh:90` invokes `$DOCKER compose -f docker-compose.yml` with **no `--env-file`.**

  So §3's *"it lives on the box, operator-owned, in a file the shipper does not write"* does not describe a reachable state. Move `ISLAND_VERSION` to any other file and Compose does not read it: it falls to the default, **`edge`, which tracks `main`.** The next `update.sh` on a box pinned to `0.15.0` would pull `edge`. Silently. Both islands.

  **And every escape reintroduces something already deleted.** Keep it in `.env` and have the shipper preserve the box's line → that is substituting one value into a shipped artifact, i.e. **templating**, the fossil v1's temper deleted. Export it → ISL-0003's drift guard already measured that an exported `ISLAND_VERSION` beats `.env` and makes the guard bless one tag while `pull` fetches another. Use `--env-file` → a second env source, which is the *"env beats `.env`"* precedence class this repo has already been bitten by.

  Flaw 1 is the design's central claim. The one-command collapse, the unreachable-by-construction FATAL, and the ungated `restore` **all descend from it.** If it does not hold, round 3's subtraction unwinds back to round 2's three commands.

- **THE KEYLESS GENERATION IS 2026-09-11 WITH THE HALVES SWAPPED.** §8 ships a compose-only generation with no `.env`, and calls that break-glass. But the incident was *a value present in one file and its forwarding absent from the other*. A keyless generation places a **new compose against the box's stale `.env`** — so a compose that forwards a variable the stale `.env` lacks interpolates to empty, and `config.py`'s all-or-none guard **refuses to boot.** That is the same failure, mirrored, produced by the mechanism advertised as the repair for it. §1 argues persuasively that `.env`-alone reproduces the incident; the same argument applies to compose-alone and the design does not notice.

- **THE SHIP/APPLY GAP IS NEW, UNBOUNDED, AND UNNAMED.** Before this design, editing compose and running `update.sh` happened in one sitting, because editing was manual and you were already there. The shipper places files and stops; the operator runs `update.sh` "when they choose." That gap is now **unbounded** and, worse, **invisible from the outside**: `/health` is green, containers are fine, and the box's compose has said something different for three days. §4 names two facts on disk honestly — but naming is not bounding. A `docker compose up` from any other cause (an unrelated tenant operation, a stray hand) applies a change nobody was ready to apply, at a moment nobody chose.

**What holds:**
- **The deletion of `deploy/**` is the round's best move** and should survive whatever happens to flaw 1. It removed the governance treadmill, the executable-cargo privilege problem, and the backup-path arithmetic **by removing their subject.** R2-2 and R2-6 have no surface left.
- **§1's cohort-of-two argument is the strongest thing in the document** and it answers Carnot's alternative on the merits: `.env` and compose are the pair that broke, the value in one and the forwarding in the other, so either alone reproduces the incident. That argument is correct — and flaw 2 is simply the same argument applied to the half the design forgot.
- **`mv -T` with both operands**, staging under `REMOTE_PATH`, fail-closed if `mv` lacks `-T`, and no plain-`mv` fallback (§4). Correct and now concrete.
- **§5a's three daemon answers**, with `aiko_data` deciding that no containers means a *stopped tenant* rather than an absence. This closes R2-3 properly.
- **§5's baseline as a file fact read from the box.** Both sides of the comparison are now the same kind of thing.
- **§8's honesty.** Accepted Risks rather than Mitigations; control-side compromise stated as the central cost rather than engineered around.

**If not SOUND, what to fold back:**
- **Flaw 1 has no cheap fix and that is the finding.** Every route either restores templating, or relies on an export path this repo has measured as a hazard, or adds a second env source. If `ISLAND_VERSION` must stay inside the shipped `.env`, then the shipper *can* change the image, and round 2's `ship-config` / `ship-release` split comes back — **which is round 2's size.** State that plainly rather than shipping a design whose central removal does not work.
- **Flaw 2:** a keyless generation must be validated against the box's **live** `.env` before placement — every variable the new compose references must be satisfied by what is already there, or REFUSE. Cheap, and it is a check rather than a mechanism.
- **Flaw 3:** bound the gap. Either `ship` ends by telling the operator exactly what remains unapplied and refusing to exit zero until acknowledged, or `GENERATION.txt` carries a placed-but-unapplied marker the next `ship` refuses to stack on.

**Procedural note, applying the skill's own rule rather than proposing a round 4.** This is round 3 of ≤3. If the panel does not reach SOUND here, the honest disposition is **UN-TEMPERED (provisional) / fall back**, not another fold. Flaw 1 in particular is not a fold-back — it is evidence that the subtraction that made round 3 smaller than round 2 **cannot be performed**, and therefore that round 2's size was the design's true size. That is exactly what Carnot's DISSOLVE predicted, arrived at from a direction he did not name.
