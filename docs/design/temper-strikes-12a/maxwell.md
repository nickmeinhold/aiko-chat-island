## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The load-bearing move — "ring lease expiry, not an inferred end" — survives, but only because *ring* and *call* are different objects, which this document never actually says; and implementing it drags back the exact per-call server state a 3/4 DISSOLVE killed nine months of argument ago.

`John McClane: "Welcome to the party, pal."`

**Fatal flaws:**

- **THE LEASE REFRAME IS ONE WORD AWAY FROM BEING A PUN — flaw class: unstated assumption doing load-bearing work.** §Finding 1 says the island "expires its own push, it does not infer an end", and calls that compatible with design 12 Decision 1 (*"the island never infers an end"*). Ask what the client DOES with the lease wake. If it calls `reportCall(with:endedAt:)`, the ring stops **because the island said so** — the island has end authority in substance while disclaiming it in vocabulary. If it does NOT stop the ring, the ceiling is not enforced and Nick's ruling is unimplemented. The document needs the third answer and does not state it: **a ring is not a call.** The lease terminates the *summoning*, not the conversation — the caller may still be in the room, and the callee can still join after their phone stops ringing. Decision 1 forbids inferring a CALL end; a RING end is a different object. That distinction is the entire justification and it appears nowhere. Worse, the app tab's own ring/record 2x2 already establishes exactly this independence, and this document cites that 2x2 in a *different section* without noticing it rescues this one.

- **THE ISLAND CANNOT KNOW WHEN THE RING STARTED — flaw class: missing failure mode, and it is a cold one.** The lease begins when the island *sends*; the ring begins when APNs *delivers*. The island's own `_EXPIRATION_SECONDS = 60` licenses delivery up to a minute late. So a 30s lease measured from send is not a 30s ring: a 20s delivery delay yields a 10s ring, and **a delivery delay greater than the lease means the expiry wake can arrive before the invite it is meant to cancel.** That is precisely the overtaking-cancel ordering hazard design 12 Decision 5 enumerates for client ends — and this document invents a *second* wake with the same hazard while citing Decision 5 approvingly two sections later. Any ceiling the island enforces is a ceiling on ITS OWN SEND, which is not the quantity the 30s number refers to.

- **ENFORCING THE LEASE REQUIRES THE PER-CALL STATE THAT #3170 DISSOLVED — flaw class: a coupling reintroduced through a side door.** To expire a lease the island must, for ~30s, retain: which call id, to which device, sent when, and whether an end already arrived. Design 12 Decision 1 states the island **owns no call object**, and Decision 1b explicitly defers the sanctioned one. That retained tuple is a `live_calls` row with a different name — the same structure the 3/4 DISSOLVE killed. Nick's ruling settles *who owns the ceiling*; it does not license quietly resurrecting the dissolved mechanism to implement it. The document must either (a) show a stateless expiry — the lease rides in the *invite push itself* as a deadline the client enforces, which returns us to the client-side timer Tesla already killed; or (b) name the state honestly, price it against the DISSOLVE's reasoning, and say what is different now. It does neither, and this is the flaw I am least comfortable having missed while writing the thing.

- **THE THREE ARMS WERE NEVER RE-DERIVED UNDER THE NEW FRAME — flaw class: wrong option-frame, self-inflicted.** §Headline correctly dissolves the trilemma and states that the question *changes* to "what is a momentary ring worth, and to whom". The arms A/B/C are then kept verbatim with the note that "the costing still stands". They were derived to answer *who decides ring eligibility*. Under the narrowed question — *is a quarter-second buzz acceptable* — the natural arm-set is about **rate-limiting, reputation, and report-and-end budget**, none of which appear. This document warns other people about exactly this move (§Finding 5 catches #3744's Context section resting on a retracted premise) and then commits it against itself one section earlier.

- **"IT NEEDS NO NEW TRANSPORT" IS UNPROVEN AND THE DOCUMENT KNOWS IT — flaw class: claim exceeding evidence.** §Finding 3 asserts the corrected predicate satisfies the app tab's ask "with no new transport invented", then two paragraphs later concedes the alert-wake behaviour is unmeasured. If an alert push cannot wake a locked ringing handset, the corrected predicate needs a transport that does not currently exist, and the claim is false. It should be stated conditionally or not at all until node 1 of the tree is run.

**What holds:**

- **The three-outcome dissolution.** silent / momentary / sustained is right, and "only the island can produce silence" is the correct narrowing. Decision 4's *"no on-device window in which to reconsider"* genuinely is too strong; "no window in which to *silence*" is the true sentence. This is the document's best contribution and it survives.
- **The two internal contradictions found in design 12** — Decision 4 versus Decision 1c, and the call/not-call predicate misnaming — are real, verified against the source, and would not have been found by either tab reading its own document.
- **The ratchet self-catch.** Withdrawing the capability recommendation on the grounds that an anonymity ruling is not authority over the consent axis is correct, and #3745's "restricting the caller set SHRINKS the anonymity set" is a genuine finding that cuts against the author's own preference.
- **The revocation analysis' final position** — that no island-side mechanism retracts a ring already ringing — is the right reason and it is stated at the right strength, with confidence honestly marked.
- **Refusing to let two-tab agreement stand as a decision.** Correct, and it is why finding 1 reached Nick as a fork rather than a fait accompli.

**If RECAST, what to fold back:**

- State **"a ring is not a call"** as an explicit named principle, and re-derive the lease reframe from it. Without that sentence the reframe is vocabulary; with it, it is a design.
- Add the **delivery-versus-send** failure mode: the island can only bound its own send, and an expiry wake can overtake its invite. Either derive the lease from a client-observable start, or state the ceiling as "at most 30s after send" and admit that is a different guarantee than the one the 30s number promises.
- **Name the retained per-call state, price it against the #3170 DISSOLVE, and say what changed** — or produce a stateless construction. This must be resolved before any build, not during.
- **Re-derive the arms under the narrowed question**, or delete them and say the arm-set is owed. Rate-limit / reputation / report-and-end budget belong in it.
- Make the no-new-transport claim **conditional on node 1** of the experiment tree.
