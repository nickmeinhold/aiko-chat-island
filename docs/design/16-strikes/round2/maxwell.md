## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The fold discharged the two data-loss flaws and the overclaims, but it introduced three new holes at exactly the seams it created — and the worst is that break-glass, as now specified, requires the very key whose loss is the emergency.

`Ripley: "Did IQs just drop sharply while I was away?"` I folded this six hours ago. Let's see what I welded shut and what I welded open.

**Did the fold discharge round 1?:**
- **Flaw 1 (backup in the pruned generation) — PARTIAL.** Absolute path plus a CI assertion is the right shape. But the design says *"a CI test asserts it"* and never says **what the test constructs**. A test that checks a path-computation function passes while the real `update.sh` does something else; only a test that actually invokes the script with cwd at `releases/<ts>/deploy` and then looks for the file on disk can fail the way the real thing fails. This is the session's own crux applied to the fix for the session's own crux, and the design is currently a predicate about a path string.
- **Flaw 2 (every island's ciphertext) — DISCHARGED for the known case.** The denylist names `deploy/secrets/**`. But "the denylist is the closed set" is a claim about a list someone maintains; §9.2 nominates the general rule as an open question rather than answering it. Discharged today, unbounded tomorrow.
- **Flaw 3 (§2/§5, the wire pulled to opposite grounds) — PARTIAL, and see the new flaws.** §2 now states the truth plainly, which was the ask. But the mitigations are weaker than they read: a second age recipient discharges single-point-of-**availability** and does nothing for single-point-of-**compromise** — a compromised control side still reaches both islands' config, and the design presents the recipient as answering both.
- **Flaw 4 (atomicity overclaim / FATAL armed) — DISCHARGED.** The sentence is replaced rather than hedged, two facts on disk are named, and the command split disarms the trigger mechanically rather than in prose. This is the strongest part of the fold.
- **Flaw 5 (drift/delivery collapse) — PARTIAL. See new flaw 2.**
- **Flaws 6–10 (daemon interrogation, human compiler, local/, cutover, hygiene) — DISCHARGED as written**, with the exceptions raised below.

**New or surviving fatal flaws:**

- **BREAK-GLASS REQUIRES THE KEY WHOSE LOSS IS THE EMERGENCY (§2).** The fold specifies break-glass as *"placing a generation and swinging `current`"* — and a generation contains `.env`, which requires decryption, which requires the age key. So the documented recovery path is unavailable in precisely the scenario §2 raises two paragraphs earlier: key loss blocking every config change. The second recipient reduces the probability and does not change the structure. **There is currently no specified action for an operator holding no key and a broken island.** Hand-editing is what the next ship refuses; the design closed that door and did not open another. A design whose emergency procedure assumes the non-emergency is not a design with an emergency procedure.

- **`baseline` HAS NO SPECIFIED HOME, AND WHERE IT LIVES DECIDES WHETHER THE FIX WORKS (§5).** The fold adopts baseline / target / live and defines baseline as *"the generation the operator believes the box is running."* **Believes** is not a storage location. If baseline is persisted control-side, it is a fourth thing that can drift — and a control-side baseline disagreeing with a live box is indistinguishable from box drift, which is the bug the three states exist to fix. If baseline is instead read from the box, it **is** `live` and the distinction collapses back to round 1's two-state comparison. The design takes Carnot's structure and omits the one fact that makes it work.

- **`ship-config`'s REFUSAL HAS NO ESCAPE THAT ISN'T THE DANGEROUS COMMAND (§4a).** `ship-config` refuses when the pin differs from what is running. Consider the live case: an operator hand-edited `ISLAND_VERSION`, or a previous `ship-release` half-completed, so the box's pin and its running image disagree. Now the *only* path that will move a config fix is `ship-release` — **which pulls**. So the state that most needs a safe config-only repair is the state in which the safe command is unavailable and the operator is pushed onto the one that changes the image. The split is correct; its failure mode routes traffic the wrong way.

- **A STOPPED ISLAND MAY BE UNIDENTIFIABLE, AND THEN FAILS CLOSED (§5c).** Identity now comes from asking docker which compose-file path project `aiko` was last started from. That is the right instrument — it asks the daemon instead of reading the shipper's own letter. But **what does it answer when the project is stopped?** If nothing, then `COULD NOT RUN` fires and fails closed, and a config fix cannot be shipped to a **stopped island** — which is the state after a failed `/health`, i.e. exactly when a config fix is what you need. Round 1 was punished for a check that cannot run being indistinguishable from a check that passed; this is its mirror, a check that cannot run blocking the repair.

- **`local/` IS "COMPOSED OVER" AND THE COMPOSITION IS UNDEFINED (§3c).** If `.env` exists in both the generation and `local/`, which wins? If `local/` wins, an operator's forgotten override silently defeats a shipped config change — the 2026-09-11 failure mode with a new hiding place. If the generation wins, `local/` does not actually preserve local state and §3c's promise is empty. The manifest-hash refusal is good and orthogonal; it catches modification of `current/`, not precedence between two live trees.

**What holds:**
- **The command split (§4a) is the fold's best work** and should survive whatever else changes. Separating "may move the pin" from "may not" means the ordinary operation cannot carry a migration, which is a structural disarm rather than a warning.
- **Bind the digest (§4b)** and **`mv -T` (§4c)** are correct, cheap, and now explicit.
- **Deleting §1's thesis line** rather than softening it was right; it was the sentence an implementer would have quoted while rebuilding auto-sync.
- **Rejecting Kelvin's `curl | bash`** in the document, with the reason, keeps a right finding from carrying a wrong fix into the next round.
- **The cutover diff restored in §8** — refuse on mismatch, neither consecrate nor clobber — closes a real hole round 1 had opened by dropping v1's gate.
- **§3a remains the honest justification** and is stated as narrower than "the class is closed."

**If RECAST, what to fold back:**
- **Specify break-glass for the keyless case**, or state plainly that there isn't one and that the second recipient is the entire answer — a named, accepted risk with an owner, not a silent gap. Candidate: allow a signed, unencrypted config-only generation (compose + `deploy/` minus `.env`) so a compose forward can ship without touching secrets. That is exactly the 2026-09-11 repair and it needs no key.
- **Say where `baseline` is persisted**, and what reconciles it when it disagrees with `live` for a legitimate reason (someone else shipped, a restore happened).
- **Give `ship-config` a pin-mismatch path that does not pull** — it can refuse to *change* the pin while still shipping config against the running digest, which is strictly safer than routing the operator to `ship-release`.
- **Specify the stopped-island answer in §5c**, including whether a stopped project can be identified from the volume or the compose file alone, and make "stopped" a distinct state from "unidentifiable."
- **Define `local/` precedence explicitly**, both directions, with the loser's existence surfaced in the pre-ship diff so a shadowing override cannot be silent.
- **Say what the CI test in §3b actually constructs** — invoke the real script, assert the file lands outside `releases/`, and treat a passing path-computation check as insufficient.
