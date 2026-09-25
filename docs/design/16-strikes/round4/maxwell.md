## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The pin left `.env` and landed in a file nothing syncs, nothing ships, nothing checks, and whose *absence silently means `edge`* — I moved the failure rather than removing it, and §3d's cutover order breaks a live box before the first ship can fix it.

`Ellen Ripley: "You don't see them fucking each other over for a goddamn percentage."` I wrote this fold an hour ago. Three of the four sentences are the ones that were asked for. The fourth built a new hole.

**Round 3 findings:**
- **R3-2 (keyless void) — DISCHARGED.** Deleted by ruling, not replaced. The second age recipient is correctly downgraded to runbook advice, and key availability leaves scope. This is the cleanest discharge across four rounds because it is a decision rather than a mechanism.
- **R3-3 (`update.sh` specified both ways) — DISCHARGED.** §4a writes the invocation out, and naming the project directory makes the inode assertion writable — *canonical parent of the written inode*, which a `printf` cannot satisfy. Both halves of the finding close together.
- **R3-1 (the pin cannot leave) — PARTIAL, and the remainder is flaw 1.** The measurement is real and correctly taken on both boxes. The pin *can* leave. What was not asked is what its new home costs.

**New or surviving fatal flaws:**

- **`pin.env` IS A FOURTH BOX-RESIDENT FILE WHOSE ABSENCE SILENTLY MEANS `edge`.** The design's entire warrant is ISL-0003's *"prose functioning as executable governance with no compiler."* §3 creates a file that **nothing ships, nothing syncs, no preflight reads, and the shipper deliberately does not write** — and `docker-compose.yml` still says `${ISLAND_VERSION:-edge}`. So if `pin.env` is missing, empty, or has the key misspelled, Compose does not fail. **It silently interpolates `edge`, which tracks `main`**, on the next `up`.

  That is the exact outcome R3-1 identified as catastrophic, re-entered through the fix for it. And it is this session's own defect class in its purest form: **a missing file and a deliberate default are indistinguishable at the point of use.**

  **The fix is a subtraction, not a guard.** Compose supports required-variable syntax: `${ISLAND_VERSION:?ISLAND_VERSION must be set in pin.env}`. **Delete the `:-edge` default.** An unset pin then **errors at `config` time**, before anything is pulled or recreated, and the error names the file. No check to write, no guard to maintain — the fallback that made absence silent is simply gone. `edge` remains available by *writing* `ISLAND_VERSION=edge`, which is a choice rather than an accident.

- **§3d'S CUTOVER ORDER BREAKS A LIVE BOX, AND THE STEP THAT WOULD FIX IT CANNOT RUN YET.** §4a's invocation references `$REMOTE_PATH/current/.env`. §3d's step 3 is *"update `update.sh`'s invocation"* — and on a box that has never been shipped to, **`current/` does not exist.** So completing the cutover leaves the island unable to deploy at all, and the first ship, which is what creates `current/`, is gated behind §5b, which is downstream.

  Worse, §3d as written removes the pin line from **the `.sops` file only**. The box's live `.env` keeps it. Both `--env-file` sources then define `ISLAND_VERSION` and merge order decides — which happens to work, and works **by accident**, because `pin.env` is passed second.

- **§7's CUTOVER DIFF IS NOW STRUCTURALLY UNSATISFIABLE.** §7 refuses the first flip unless the decrypted sops bytes match the live `.env`. After §3d removes the pin from the sops file and leaves it in `.env`, **they differ by exactly one line by design.** So the gate either refuses every first ship forever, or acquires a special case — *"ignore `ISLAND_VERSION`"* — and a byte-exact comparison with one permitted exception is the beginning of the field-by-field comparison v1's temper deleted.

  **The clean answer is another subtraction:** §3d removes the pin from **both** the `.sops` file **and** the box's live `.env`. Both sides lose the line, the diff is exact again with no exception, and `pin.env` becomes the **sole** definition rather than the winner of a merge. That also removes the accidental-merge-order dependency in flaw 2.

- **`working_dir != realpath(current)` has a benign false positive, worth one sentence not a mechanism.** Any legitimate one-off `compose up` from another directory — during the cutover, for instance — leaves labels that read as permanently half-applied until the next proper `up`. I think that is *correct* behaviour rather than a defect (it is a genuine divergence), but the design should say so, or an operator will read the first false alarm as a broken discriminator and stop trusting it.

**What holds:**
- **Nick's ruling is the strongest single move in four rounds**, and it is worth naming why: it discharged a fatal finding by *removing something from the design's scope* rather than by building. `local/`, the keyless ship, and the second-recipient requirement are all gone the same way. **Every genuine simplification in this design has come from deciding what is not ours.**
- **The measurement is sound and correctly scoped** — both boxes, both versions, the actual merge behaviour rather than the documentation's claim.
- **§4a's inode assertion** is the right shape and the right lesson: round 2's gate could be satisfied by `printf`, so this one is specified against an inode.
- **§5b makes the first ship reachable.** Kelvin's Catch-22 is genuinely closed: a box with containers and no `GENERATION.txt` is now a named state rather than two contradictory refusals.
- **§9's honesty about the page count.** The mechanism count fell and the page count rose; stating that as a finding rather than a defence is correct, and §10.5 asks the sharp version.

**If not SOUND, what to fold back:**
- **Delete the `:-edge` default from all three `image:` lines** and use `:?`. This is the round's most important change and it is a removal. Absence of the pin must be loud at `config` time, before the pull.
- **Re-order §3d and state its invariant**: the invocation may only be updated *after* a successful first ship, or `--env-file` must tolerate a missing `current/.env` on the genesis path. Name which.
- **§3d removes the pin line from BOTH the sops file and the live `.env`**, so §7's diff stays byte-exact with no exception and `pin.env` is the sole definition.
- **One sentence on the discriminator's false positive**, so its first benign firing does not train an operator to ignore it.

**Note on the round budget.** This is round 4 of an extended process. Three of the four findings above have fixes that are deletions, and the fourth is one sentence — so I do not read this as evidence the design cannot converge. But I have now said "almost there" four times, and that is itself data.
