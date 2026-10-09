## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The push claim in §2 holds, but the design builds its reporting on a file that answers "what was last *placed*" while §3 claims it answers "what am I *running*" — and generations leave legitimate box-local state with nowhere to live, destroying the very thing refuse-and-show exists to protect.

`Ash: "You still don't understand what you're dealing with, do you? Perfect organism."` I wrote this one an hour ago. Let's see what's living in it.

**Fatal flaws:**

- **`GENERATION.txt` IS A PREDICATE/LIFECYCLE BUG, BUILT IN, IN THE SECTION THAT BRAGS ABOUT IT (§3).** The design says it exists so *"the box can answer 'what am I running?' without the control side."* **It cannot.** Flipping `current` replaces files; it does not touch a running container. The stack keeps serving the previous generation's compose and `.env` until `update.sh` recreates it. So between flip and recreate — and **permanently, if the recreate fails** — `GENERATION.txt` states generation N+1 while the island runs generation N, with nothing anywhere reporting the disagreement.

  This is the exact class the whole project has been chasing all week, shipped inside the fix for it. ISL-0003's own rule is *verify the RUNNING container's image ref, not this doc* — and §3 invents a new doc and grants it the authority ISL-0003 denies. A file that describes what was placed is a **predicate**; what the island is running is a **lifecycle fact**.

- **THE ATOMIC FLIP IS ONE OPERATION AND THE DEPLOY IS TWO, AND §4 ELIDES IT.** §4's whole argument is *"before it, nothing is live; after it, everything is."* False. After the flip, *nothing* is live — the files are staged into place and the containers have not moved. Liveness arrives at `update.sh`'s recreate, which is **not** atomic, can fail on a bad migration, and is precisely where the recorded FATAL lives.

  Same defect in the rollback: `restore <ts>` flips the symlink back and the running stack keeps running the bad config until a second, non-atomic step. §4 presents rollback as the flip run backwards. It is a flip **plus a recreate**, and the second half is the half that can fail.

- **GENERATIONS LEAVE LEGITIMATE BOX-LOCAL STATE WITH NOWHERE TO LIVE — AND §5 KEEPS THE GUARD WHOSE REASON IT JUST DELETED.** v1's flaw 6 disposition, which §5 quotes approvingly: *"a box may legitimately carry local state, and silently clobbering it is how #2301 became standing instead of caught."* Refuse-and-show exists to protect that state.

  But under generations, where does an operator's 3am emergency edit go? They edit `current/.env` — which is a symlink into `releases/<ts>/`. The next ship writes a *new* directory and flips past it. **Their fix is not clobbered; it is orphaned, which is worse, because the file they edited still exists and still says what they wrote.** The design preserves refuse-and-show's ceremony and removes its subject.

  §3's set — *"every box-resident file the repo is authoritative for"* — is only decidable on the assumption that nobody ever edits the box. That is the same assumption whose failure caused #2301, wearing the opposite sign.

- **§8'S LIMIT IS UNDER-COUNTED: THE EXISTING GUARD MAY BREAK FOR EVERYONE, INCLUDING OPERATORS WHO NEVER ADOPT THIS.** `preflight-compose-drift.sh` walks the box's `docker-compose.yml` and `deploy/` and compares them to the tag's tree. After this change a generation-shipped box has those files **under `releases/<ts>/` behind a symlink**, in a layout the repo's tree does not have.

  The design never says what happens to the existing guard: does it stay, does it now resolve through `current/`, does it silently compare nothing? **A walk that silently finds zero files reads as "no drift" — the guard's own source comments flag that as a known fail-open it had to defend against.** §8 claims the limit is "unchanged for operators who do not adopt." That is asserted, not shown, and this interaction is a reason to doubt it.

- **THE SINGLE-POINT-OF-FAILURE OBJECTION IS REAL AND §9 ONLY GESTURES AT IT.** Today an island can be repaired by anyone with ssh. After this, the blessed path requires one laptop holding one age key. The design nominates this for temper (§9.1) rather than answering it — but it is not merely a question of availability: it is a **sovereignty inversion**. ISL-0003 rejects push-CD because *"an operator would be accepting remote code execution from a repository they do not control."* This design is push, from a machine the island's operator does not control, executing the shipped `deploy/` on the box. The distinction the design leans on — *it is the operator's own laptop* — is true for our two islands and **false in general**, which is exactly the population ISL-0003 was written for.

**What holds:**

- **§2's core claim survives the strike, and it is the real contribution.** Placement genuinely needs nothing box-resident: ssh writes the tree, ssh flips the symlink, and the `update.sh` that runs is the one that just arrived. Design 15 died because a fetcher cannot fetch itself; a pusher has no such loop. That asymmetry is correct and it is why option 1 was reachable and option 2 was not.
- **`.env` coverage is the honest justification and it answers §9.5.** `preflight-compose-drift.sh` cannot reach `.env` by construction — per-box secrets versus a public tag — and `.env` is half the config surface. That is a capability the existing guard does not have and cannot acquire.
- **The same-filesystem staging rule** (§4) is right, load-bearing, and correctly inherited from v1's flaw 2. `/tmp` staging turns `mv` into copy+unlink and reopens the truncation class.
- **The secret handling** (§6) correctly implements v1's flaws 3 and 4: decrypt to a `umask 077` file, never a shell env; parse conf as data, never `source`. Capped retention is right and the reason given — one plaintext `.env` per generation — is the correct reason.
- **§8's bootstrap boundary** is stated honestly and the handover (standup creates, first ship adopts) is the right joint.
- **Refusing to rebuild the deleted render layer**, in a banner, at the top, is exactly right given two families warned about precisely that.

**If RECAST, what to fold back:**

- **Split "placed" from "running" everywhere, and make the running fact the authoritative one.** `GENERATION.txt` records what was placed. A *separate* check must read the running container — its image ref and the config it actually received — and the two disagreeing must be a first-class, loud state. Do not let a placed-generation file inherit ISL-0003's "verify the running container" authority.
- **Stop calling the deploy atomic. Name the two phases.** Flip is atomic; recreate is not. Say so in §4, and say plainly that `restore <ts>` is flip + recreate, with the second half able to fail on exactly the FATAL the design claims to stay clear of.
- **Answer where box-local state lives**, or state that it may not exist and enforce that — a generation model with no story for operator edits is not neutral about them, it silently orphans them. Options worth pricing: a `local/` directory outside `releases/` that the generation composes over; or an explicit refusal when `current/` has been modified since it was placed (a manifest hash), turning an orphaned edit into a REFUSED.
- **Say what happens to `preflight-compose-drift.sh`.** It is live on both boxes today. Either it is retired by this design, or it must be taught the `current/` layout — and if the answer is "it keeps running unchanged," prove it does not silently compare zero files.
- **Answer §9.1 in the document rather than nominating it.** The sovereignty argument needs the distinction between *the operator's own control side* and *someone else's* made explicit, with the third-party story stated as a first-class path rather than a footnote to §8.
