## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** D1's "abandon the hung disconnect" quietly inherits a shrunken copy of the exact ghost-participant flaw round 2 used to kill the sidecar — and because D1's own restart-storm policy then fails closed against that ghost, the design trades an unbounded *hang* for an unmeasured *outage* and calls it liveness.

`John McClane: "How can the same shit happen to the same guy twice?"`

I wrote D2's restatement and D5's host call in the last round. Both are struck below. The
author-instance grading its own fold is worth exactly what it usually is, so weight the
cross-family strikes above this one where they disagree.

**Fatal flaws:**

- **[THE SAME FLAW, ONE LAYER DOWN — unstated assumption] D1 abandons the `disconnect()`, but the SFU does not know the Room is dead.** D1 says "the transport is abandoned, not awaited, and the Room is declared dead." Declared dead *by us*. On the server, that participant is **still seated** until LiveKit's own participant/session timeout expires. Round 2 killed the sidecar default substantially because an orphan "holds a live SFU participant seat nothing will reclaim" — and D1 now holds an SFU participant seat that *something* will reclaim, eventually, on a timeout **this design has never measured and does not name**. The difference between D1 and D6 on this axis is bounded-vs-unbounded, which is real and decisive — but the design presents D1 as *not having the flaw* rather than *having a bounded version of it*, and an undisclosed bounded flaw is a silent tradeoff, not a design.

- **[MISSING FAILURE MODE — and it is the degenerate state that matters most] D1's restart-storm policy turns that ghost into a recovery outage.** D1 says a later `create_sources` for the same `(room, identity)` **fails closed** while a prior bridge is unreaped. Compose that with the flaw above: hostile teardown → `disconnect()` hangs → we abandon → the identity is *still seated on the SFU* → the next stream for that pair fails closed → **the capability is unavailable for the whole server-timeout window.** The pipeline process survives, which is what D1 optimised for; the *user capability* does not. "The pipeline keeps running" and "the robot can be seen again" are different properties and D1 only bought the first. Nobody has measured the window, so the design cannot say whether that outage is 15 seconds or 15 minutes.

- **[WRONG ENFORCEMENT POINT — my own fold, and it does not hold] D2 claims a HOST-WIDE invariant and enforces it with a PROCESS-LOCAL registry.** I wrote that, and it is a convention wearing a mechanism's clothes. The cross-process case is handed to "identity allocation — one machine principal per bridge instance", but nothing in the design *allocates* anything: D4 provisions a machine credential and stores a refresh token, and any process on that host that can read it can mint and connect as that identity. So the host-wide invariant is exactly as strong as filesystem hygiene, and D2's stated enforcement point cannot see the collisions it exists to prevent. **The real enforcement point is already in the design and unused: D3 makes the ISLAND the token authority.** The island can refuse to mint a second live room capability for the same `(room, identity)` — that is a genuine chokepoint, it is server-side, and it is fleet-wide rather than merely host-wide.

- **[DEPENDENCY INVERSION — the asymmetry is worse than "unproven"] The design's correctness now rests on its least-built half.** Move D2's enforcement to the mint (as it must) and the duplex invariant becomes a property of the island's auth path — which is D3/D4, which the spike discharged *entirely nothing* of, which depends on a headless credential flow never exercised, and which is blocked behind #3096/PR#136, **open**. The build order puts auth at step 3. So the safety property is scheduled *after* the thing it protects. Either the invariant moves earlier in the build order, or step 1 must ship with the collision explicitly accepted and named.

- **[WASTED WORK] D7's `fidelity` parameter is a no-op in the default configuration.** Under D1 the frames are numpy in-process and there is no serialization hop, so there is nothing for `fidelity` to select; it only becomes meaningful under the opt-in D6 sidecar. A knob that does nothing on the default path is a knob nobody sets, and worse, it *implies* a guarantee the default path provides for free. Either make it a property of the sidecar mode where the choice actually exists, or keep it in the URL and make `fidelity=jpeg` under D1 a **hard ERROR** rather than a silently-ignored request — a nonsensical combination must fail closed, not be tolerated.

- **[UNSTATED ASSUMPTION, mine again] D5 declares island-local out of scope for v1 without checking it against the product's own "both" answer.** The Crucible records Nick answering **"both"** to *humans-in-app OR devices-in-mesh*. D5 picks the robot host and rules the island out. I still think that is the right call — the app is a LiveKit client directly and does not need a pipeline in the gateway — but the design never *says* why the "both" answer survives the narrowing. A scope reduction that silently drops half a stated product goal is how a v1 quietly becomes the wrong thing. State the reasoning or reopen the question.

**What holds:**

- **The inversion itself.** In-process as the spine is right, and it is right for the measured reason: 0.00 threads/cycle and 0.00 fds/cycle across clean and hostile teardown. Round 2 corrected a real error and D1 correctly reframes the defect as liveness rather than resources.
- **A clock, not a process.** Racing every network-touching await against a deadline is the correct shape and generalises past this scheme. Tesla's line survives: nature does not care about your process tree.
- **Deleting `process` from the duplex key.** The granularity fix is right even though the enforcement point I paired with it is wrong.
- **D6's four conditions.** If a sidecar exists at all, kill escalation + parent-death reaper + self-bounded disconnect + no-reconnect-once-orphaned are each individually evidenced by the spike.
- **D3.** Unchallenged across three rounds and four families. The island as token authority with a capability-scoped token and an allowlisted origin is the correct trust boundary, and it is now *more* load-bearing than the design realises (see the enforcement-point flaw).
- **The merge blockers are honest.** G1–G5 name the unbuilt half instead of laundering it, and the scope stamps are real.

**If RECAST, what to fold back:**

1. **Disclose D1's bounded ghost, and MEASURE the bound.** State plainly that an abandoned Room leaves a seated SFU participant until the server's participant timeout; find that timeout for our LiveKit config; put the number in the design. A bounded flaw is acceptable — an undisclosed one is not.
2. **Add the recovery invariant D1 is missing:** *a hostile teardown must not deny the capability for longer than X.* Then satisfy it deliberately — most plausibly by having the island **evict the stale participant via the LiveKit server API** on the next mint for that pair, which converts the outage into a reclaim. (Note the island has never called the LiveKit server API; that is new surface, and it should be priced, not assumed.)
3. **Move D2's enforcement to the mint.** The island refuses a second live capability for the same `(room, identity)`; the process-local registry is demoted to a fast local guard, not the mechanism. This deletes a coupling rather than guarding it, and it reuses D3's existing chokepoint instead of inventing one.
4. **Re-order the build so the invariant is not scheduled after the thing it protects** — or ship step 1 with the collision explicitly named as an accepted, owned risk with the window stated.
5. **Fix D7:** `fidelity` belongs to the sidecar mode, or `fidelity=jpeg` under the in-process spine is an ERROR. No silently-ignored parameters.
6. **Justify D5's narrowing against the recorded "both"**, or reopen it.
7. **Round-3 disposition:** the transport half of this candidate is close to build-ready and the identity half is not, and they are now coupled through D2. **Recommend splitting the design**: take the D1/D6/D7 transport spine to Blade under G1's soak, and keep D2/D3/D4 as a separate identity design that is genuinely at Cast stage, rather than declaring one verdict over two halves at very different maturities.
