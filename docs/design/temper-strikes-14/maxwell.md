## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST — with one flaw I could not talk myself out of, and it aims at the load-bearing beam.

**Summary:** The stateless ACL works only for a call whose participant set is FIXED AT CREATION, which is not a gathering — so design 14 may answer the gatherings ruling with a mechanism that supports only the 1:1 case it was written to move past.

`Hudson: "Game over, man. Game over!"` Not yet. But §2b is standing closer to the edge than the document admits, and I wrote the document.

**Fatal flaws:**

- **F1 — THE WIRE OBJECT HAS NO ORDERING, and a gathering needs one. (missing failure mode / the beam itself.)** §2b says the ACL is `f(signed invite set, policy, occupancy)` and stores nothing. That holds for the set named at creation. **Now add someone.** "Gather people for a thing" means the set can grow — that is the entire content of the 2026-08-17 ruling. Adding a participant means a SECOND signed object naming a different set for the same call ULID, and **the island cannot tell which is later.** Client timestamps are not trustworthy for ordering, and there is no state to compare against. So the island must either accept any superset (and then **removal is impossible**, killing mid-call moderation — you cannot eject anyone, ever) or reject all amendments (and then the set is fixed at creation, which is not a gathering). A database row has monotonicity for free; that is most of what a row IS. **If this does not resolve, §2b's "function, not a table" is a claim that only survives in the 1:1 case, and the design fails at its own purpose.**

- **F2 — §4 never justifies its premise: that permanence is a DEFECT. (wrong option-frame — the illegal move.)** The section argues the invite's permanence "was never chosen, it is inherited from the transport", and treats that as self-evidently bad. **It is not.** A call log is something every telephone has had for a century and every user expects. Missed calls, "who rang me at 3am", "call them back" — these are *features*, and they are exactly what a permanent record in channel history provides for free. The document frames the choice as message-vs-wire-object when the real option space includes **message AND call-id**, which keeps the record and still fixes #4265. §4 smuggled "ephemeral things must not leave permanent records" in as an axiom, and it is the least examined sentence in the document. Nick's no-refused-ring-record ruling is cited in support, but that ruling is about **blocked** rings — a deliberately different case from a call that actually happened — and using it here is the same axis-upgrade the author was caught committing on arm (C) four hours ago.

- **F3 — §3 may re-commit temper flaw 3 one level up. (unstated assumption.)** The clock collapse rests on "the island **sets** `expires_at`, the device **enforces** it". Nothing makes the device enforce it. An older build ignores an unknown field; a hostile client ignores it deliberately. So "the island owns the ceiling" degrades to "the island *suggests* the ceiling" — which is precisely what temper flaw 3 struck as *"a rename unless the wire carries the distinction"*, now recurring with the rename one layer up. The document claims this REMOVES flaw 3's murder-a-live-conversation hazard; what it actually does is move the failure from "island stops a live call" to "client never stops a dead ring", and it does not say so.

- **F4 — the room rename's blast radius is unpriced. (under-counted blast-radius.)** §6 prices the v2 invite as the one-way door and never mentions that `room_for_call(ulid)` replacing `room_for_channel(channel.id)` is a **media-plane** change. It touches the token mint, the occupancy endpoint, `island_id` namespacing, the app's join path, and it lands on two live islands whose LiveKit configs are hand-edited and structurally divergent (#3685). A wire-format change and a room-naming change are different kinds of irreversible and the document counts only one.

- **F5 — §0's table is rhetorically stronger than it is true. (the ratchet.)** "Seven findings, one absence" is a satisfying sentence and it overstates. Temper flaw 4 is NOT caused by the absence of a call object: `12a-MEASURED` M4 established that the 60s APNs TTL and the 10s client freshness are a **deliberate, reasoned architectural divergence** — *"both are right for their own architecture"*. I folded a measured design decision into "one absence" because it made the case tidier. That is the escalation-under-reuse pattern this very bundle documents, committed inside the document that documents it.

**What holds:**

- §0's core observation is real and I have not seen it stated anywhere else in either repo: **a call today is a magic string spoken into a channel**, with no object in the DB, on the wire, or in the room name. Even if every proposal here dies, that sentence is worth keeping.
- §2a's co-presence hole is genuine and independently corroborated: per-edge authorization of *invitation* does not authorize *co-presence*, and the codebase already knows this — it is the stated reason DM-only exists. Any gathering design owes an answer.
- §2c's separation (invite carries IDENTITY, island supplies POLICY re-read fresh) is the property that avoids inheriting arm (C)'s revocation disqualifier, and it is correctly distinguished from arm (C) rather than conflated with it.
- §5 is doing real work: it names what the design does NOT buy, including the third cell, which the author reached for twice in ninety minutes.

**If RECAST, what to fold back:**

1. **Answer F1 or narrow the claim.** Either show how a growing participant set stays orderable with no island state, or state plainly that the stateless construction covers a **fixed-set call** and that gatherings-with-a-growing-set need state — and price that state against #3170's DISSOLVE, which is what temper flaw 10 asked for and what this document claimed to have escaped.
2. **Justify or drop §4's premise.** Argue that permanence is a defect, or add the third option (message AND call-id) to the frame and let it compete.
3. **§3: name the enforcement.** Say what happens when the device does not honour `expires_at`, and stop claiming flaw 3 is removed when it is relocated.
4. **Price the room rename** in §6 alongside the v2 invite, as a separate irreversible on the media plane.
5. **Fix §0's table** — remove flaw 4 from it, or mark it as partially caused, citing M4.
