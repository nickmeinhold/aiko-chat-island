## KelvinBitBrawler's Design Strike

**Verdict:** RECAST

**Summary:** The design correctly identifies the central trilemma and its dissolution into three outcomes, but the absolute zero of its core premise — that the "momentary ring" is a stable state — is reached when its own un-solved failure modes increase the rate of that event toward a systemic phase transition.

**Fatal flaws:**
- **Shared-Premise Failure / Un-costed Risk Transfer:** The design's spine is the dissolution of the ring trilemma, resting on the "momentary ring" (Arm B) being a viable, low-rate event. This premise is made unsafe by the un-solved **key-set freshness** problem for multi-device users (App Design 16 §1c). Proposing "Accept and disclose" is not a solution; it's a transfer of risk to the user for individual failed calls, and an un-costed accumulation of risk for the entire platform (VoIP revocation from Apple due to a high report-and-end ratio). A design that knowingly increases the rate of its own primary failure mode without a mitigation is fundamentally unsound. `GLaDOS: "This next test is impossible."` The design accepts a condition that could lead to systemic failure, and the escape hatch (Arm C) is correctly identified as having its own fatal flaws.
- **Mis-Framed Revocation Dilemma:** The disqualification of the capability arm (C) leans on the mid-ring revocation problem (#3521), claiming island-side revocation cannot solve it while device-local consent can. However, #3521 exists *with* device-local consent, proving the current client implementation fails anyway. While Arm (C)'s revocation model is indeed weaker (caller holds the token), the argument as framed misrepresents the state of Arm (B), which does not solve this case either. The true, and sufficient, disqualification of (C) is the fundamental inversion of control over who may initiate a wake.

**What holds:**
- The **three-outcome dissolution** (silent / momentary / sustained) is the correct framing of the problem space. The original "no on-device window" claim was a cold fault of its own. `Roy Batty: "I've seen things you people wouldn't believe..."` And one of them was a time interval pretending to be a point.
- The **ring lease expiry** reframe for the 30s ceiling is a sound and elegant way to preserve the "island never infers an end" constraint while moving enforcement to the correct layer. It's a textbook example of correctly assigning state ownership.
- The corrected **send-path predicate** (`ring-starting / not-ring-starting`) is the right fix, and the discipline to defer hardening it pending a cheap experiment is exemplary. Measure, don't theorize at the heat death of the universe.
- The **client-minted UUID contract** is the correct, minimal-authority approach, keeping the island as a cold, dumb pipe.
- The synthesis of the **momentary ring flash and the spurious Recents entry** as two views of the same underlying event is a sharp piece of analysis.

**If RECAST, what to fold back:**
- The multi-device key-freshness problem in App Design 16 §1c cannot be left as "Accept and disclose." A technical solution MUST be designed and costed before this design can be considered sound. Options to explore include a silent push to wake other devices on consent-change, or a grace window for verification that does not require a report-then-end cycle. The current design accepts an unknown level of risk; the recast must quantify and mitigate it before the whole system suffers freezer burn from Apple.
- The framing of the argument against Arm (C) in Island Design 12a must be corrected to focus on the fundamental control inversion, not on the mid-ring revocation case, which is a bug in the client implementation affecting both arms and must be fixed independently.
