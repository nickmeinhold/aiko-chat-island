RECAST

This design is a masterclass in research and rigor, correctly reframing an ambitious but untenable thesis into a set of concrete, shippable tiers. The forge did its job. However, the central proposal that emerged—the signed ack—is built on a foundation of sand. The design must be recast to address a fatal flaw in its threat model before it can be built.

**§1. The central mechanism defends against an adversary who does not exist.**

The design’s most critical flaw is the one it admits openly in §3: **"An island cannot be MADE to sign."** This single sentence hollows out the core value of Tier 2 (signed ack) and Tier 3 (conformance prober). The mechanism defends against an operator who is both malicious enough to tamper with message history but also compliant enough to opt-in to a system that creates signed, self-incriminating evidence of that tampering. This is not a real adversary. The stated threat model is a hostile operator, and a hostile operator will simply strip the signature, making them indistinguishable from an un-upgraded island.

The Spark movement found the answer in Kelvin's "Receipt Weaving": a proof that is **load-bearing for the island's own operation.** The design discarded this property because of the privacy leak from a public log (`DESIGN.md` §6), but in doing so, it threw away the key to the entire mechanism. A voluntary proof is theatre.

**The recast:** Halt work on Tier 2 and 3. The design must return to the principle that misbehavior is *self-defeating*, not merely detectable. An island must be unable to function correctly without producing honest, signed artifacts. This could mean client-side enforcement (clients refusing to process further actions without valid signed acks) or a clever chaining mechanism that doesn't rely on a public log. Without this, the signed ack is a security feature that only works on attackers who consent to be caught.

**§2. Tier 1 is a critical bug fix and must be severed and shipped immediately.**

The claim that Tier 1 is "worth doing even if everything else dies" is **true**. The analysis in `FOLD.md` is correct: the write path enforces a `client_msg_id` binding that the read path discards, creating a verifiable relocation attack vector (`RESEARCH-crosstab.md`, C5). Echoing the row's `client_msg_id` in `message_view` is a simple, correct, and necessary fix that makes a write-time invariant checkable by any reader. This is not a feature, it is a defect repair. It should not be bundled with the speculative work in other tiers.

**§3. The privacy claim for `client_msg_id` is an unverified hand-wave.**

The design's claim C-5 that echoing a client-chosen ID to every channel member is "probably fine; not verified" is unacceptable. This broadcasts an unvalidated, client-controlled value to all members of a channel, forever. While a UUID might be fine, the design has no control over what a client puts in that field. It creates a potential information leak that directly contravenes the project's established rule against learning the social graph. Before Tier 1 ships, the risk must be analyzed, and the client specification must be updated to mandate that `client_msg_id` be a high-entropy, non-correlatable value. "Probably fine" is not a substitute for a security review.

**§4. The reframe is honest; the result is still too weak.**

To its credit, the design is not a retreat dressed as rigor. It is an honest and well-documented response to a brutal research pass that correctly invalidated the initial thesis. The pivot from "dissolve reputation" to "sign the ack you already send" is a textbook example of a crucible doing its job. The problem is not the process, but the destination. The author shrank the scope until it was shippable, but also until it was largely toothless against the stated adversary. The honest answer was small, but it might also be useless.

**§5. Tier 0 is the most valuable finding in the entire bundle.**

The discovery that the person who asked the original question has been waiting six weeks for an answer that is already written (`RESEARCH-crosstab.md`, C8) is a finding of the highest order. It is not an evasion. It is a stark reminder that engineering is a social and communicative process. Shipping Tier 0 unblocks a stakeholder and a two-month-old ticket for the cost of one conversation. That is infinite leverage.

**Verdict:**

1.  **Execute Tier 0 immediately.** Show Andy the answer.
2.  **Implement Tier 1 immediately, but as a standalone bug fix.** Before merging, properly analyze and mitigate the privacy implications of echoing `client_msg_id`.
3.  **Halt and RECAST Tier 2 and 3.** The signed ack as designed is not a viable security mechanism. The work must return to the principle of a load-bearing proof that makes misbehavior structurally impossible or self-defeating for the island, not merely voluntary to report. The current design fails to defend against its own threat model.
