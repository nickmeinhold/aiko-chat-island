Ripgrep is not available. Falling back to GrepTool.
## KelvinBitBrawler's Design Strike

**Verdict:** RECAST

**Summary:** The design correctly retreats from its own fatal flaws but lands in an unstable equilibrium, trading catastrophic failures for a series of subtle, chilling compromises that sacrifice correctness for a quiet life.

**Fatal flaws:**
-   **The reversal of ticket #3170 is a rationalization, not a superior design.** (DESIGN-v2, "Why this is still...", Claim 4). The ticket was right: a call needs its own room to prevent state leakage. This design accepts the "zombie participant" bug (FOLD-v2, S3) by calling it "not a regression." This is a known, user-facing flaw, deliberately left unfixed because the correct solution was deemed too difficult. A design that normalizes known bugs is unsound at absolute zero. `Roy Batty: "I've seen things you people wouldn't believe. Attack ships on fire off the shoulder of Orion."` I have seen state leaks bring down systems you people wouldn't believe. This is one of them, in embryo.

-   **The core safety invariant is a suggestion, not a fact.** (DESIGN-v2, Claim 1 & 2; FOLD-v2, S2). The claim that "`live` is a LABEL, never a GATE" is a prayer offered to future developers. A boolean liveness flag on a call-state endpoint is an attractive nuisance; it *will* be used to gate a capability, and the moment it is, the stale-row failure modes this design claims to have fixed will re-emerge. A safety model that relies on eternal adherence to a comment in a design doc is not a safety model; it is a future bug report.

-   **The design chooses ignorance over responsibility.** (DESIGN-v2, "Data"). Deleting the call row on end to avoid being a "phone company" (F12) is an act of thermal desorption that vents valuable diagnostic data into the void. An involuntary end caused by a `max_duration` sweep is now indistinguishable from a graceful hangup. This is not "zero retention by construction"; it is "zero knowledge by design." We are engineers; we keep logs.

-   **The island's work is misrepresented.** (DESIGN-v2, Claim 5). The claim that "Increment 0 is app-side and needs nothing from us" is false. The island must be modified to observe this new signed sentinel to trigger the deletion of the `live_calls` row. It is a small change, but claiming it is "zero island change" is a fabrication that lowers the confidence in every other claim the design makes. Precision is not a courtesy.

**What holds:**
-   The demotion of the row from ARBITER to RECORD is the correct, life-saving insight from the v1 strike. Decoupling the camera path from a non-federating SQLite row was the right move.
-   Keeping `room_for_channel` as the single, stable source of the room name correctly dissolves the federation/split-brain flaw (F2). There is no second room. That metal is good.
-   The refusal to re-open `kCallInviteBody` remains correct.
-   The analysis in FOLD-v2 is sharp, particularly the attempt to dissolve the feature entirely (S6). It correctly identifies the minimal, irreducible value the island *could* provide.

**If RECAST, what to fold back:**
1.  **Re-litigate the reversal of #3170.** The zombie participant bug is a fatal flaw, not a tolerable quirk. Either prove that a per-call room is impossible for reasons other than "the v1 design was bad," or return to that shape and solve the (federation, discovery) problems correctly this time. Do not accept a state-leaking abstraction.
2.  **If you insist on the eternal room, remove the attractive nuisance.** `GET /call` must not return a simple `live: boolean`. A design whose safety depends on its clients ignoring its output is a paradox. Return a `last_activity_at` timestamp, or a participant count that can be stale, but not a binary switch that begs to be wired to a capability gate.
3.  **Stop deleting the history.** Reintroduce `ended_at` and an `ended_reason`. The "phone company" problem is a data retention *policy* problem, to be solved with a retention sweep. Do not solve it by destroying the flight recorder.
