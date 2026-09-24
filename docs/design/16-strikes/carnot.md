## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** no real engine matches the Carnot cycle; a reviewer's job is to say how far short we are. Section 2 mostly survives: PUSH really does dissolve design 15's bootstrap loop for ongoing deploy, because the old box code is no longer the vehicle that must fetch its own replacement. The entropy is not gone, though; it moved into release selection, operator-key custody, and the adoption path. Section 9 question 5 does not dissolve the design by itself: `.env` coverage plus synced `deploy/` is real value beyond `preflight-compose-drift.sh`. But the implementation as written has a thermodynamic leak: its refuse-and-show diff appears to compare current against the generation being shipped, which makes every intentional config change look like drift. Recast around an explicit baseline/target distinction.

**Fatal flaws:**
- §5 collapses drift detection and change delivery. If the control side diffs the box's current generation against the generation about to ship and refuses on difference, then a legitimate compose/deploy/.env update is indistinguishable from unauthorized local drift. Dijkstra would call this a specification bug, not an implementation detail: the predicate does not name the state it is proving.
- §2's 'nothing of ours on the box' claim is directionally true but overstated. First adoption still depends on interpreting existing box reality, and ongoing operation still assumes ssh, docker, path layout, compose authority, and readable current state. This is not design 15's circular fetcher, but it is not frictionless reversibility either.
- The design says 'stop detecting drift, and remove the thing that drifts,' then keeps a pre-ship drift refusal. That contradiction matters. Either the generation ship is authoritative overwrite with human review, or it is a drift gate that requires pre-existing sync. Trying to be both recreates wasted work: a detector in front of a mechanism that was supposed to delete the need for the detector.
- Question 5 remains underpaid. For two known boxes, after `preflight-compose-drift.sh` ran clean on 2026-09-21, the delta over one manual `deploy/` sync per box is chiefly `.env` authority and future-proofing. That can earn a small control-side shipper, but not a broad deploy framework with ambiguous refusal semantics.

**What holds:**
- PUSH delivery does dissolve design 15's fatal bootstrap loop for ongoing deploy. New deploy tooling can arrive as cargo from the control side; old box-resident deploy code no longer has to fetch, verify, or install its successor. That is the right Carnot step: remove the irreversible coupling rather than guard it harder.
- Keeping the age key control-side still matches ISL-0003's sovereignty boundary. The operator laptop as a powerful control plane is not a new philosophical violation; it is already where SOPS authority lives.
- Complete encrypted `.env` per island is still the subtractive middle. No template, no `envsubst`, no byte-match proof theatre. Feynman's ghost approves: do not fool yourself by proving the wrong thing very carefully.
- Generation directories plus one symlink flip are the right atomicity primitive. v1's multi-file `mv` was entropy with a moustache; this is the cleaner machine.
- Tenant preflight, strict manifest parsing, fail-closed COULD NOT RUN, capped plaintext release retention, and 'restore is config rollback only' all carry forward. These are not ornament; they are the heat shields.

**Fold back:**
- RECAST name: baseline-addressed control-side generations.
- Separate three states explicitly: baseline, target, and live. Baseline is the generation the operator believes the box is currently running. Target is the new generation to ship. Live is what ssh actually reads from `current`. Refuse only when live != baseline, because that is drift. Show target-baseline as the intentional change for human review, but do not call it drift.
- For first adoption, require an explicit `adopt` mode: read live files, generate `GENERATION.txt`, and commit or record that as baseline before any overwrite. No pretending a pre-generation box can satisfy generation invariants retroactively.
- Keep the delivery mechanism small: decrypt complete `.env`, assemble release under same filesystem, verify tenant identity, flip `current`, run `current/deploy/update.sh`, prune old releases. Do not grow schedulers, attestation machinery, template engines, or clever auto-rollback.
- For question 5, write the economy test into the design: if the implementation exceeds a small control-side shipper plus restore, fall back to one manual `deploy/` sync per box plus the existing preflight. Hamming's question applies: what is the important problem, and are you working on it? The important problem is unsynced box-owned artifacts, not a majestic deploy cathedral.
