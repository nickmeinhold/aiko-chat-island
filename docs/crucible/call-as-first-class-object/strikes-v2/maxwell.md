## MaxwellMergeSlam's Design Strike

**Verdict:** DISSOLVE

**Summary:** `Roy Scheider: "You're gonna need a bigger boat."` — except we don't; I checked the water and there's no shark. The two facts I said only a server could know turn out to be one false claim and one already solved by a 30-second timer that shipped this morning.

**Fatal flaws:**

- **M1 — The federation claim in DESIGN-v2 is FALSE, and it was the headline benefit.** *(class: unstated assumption, verified against the running system)* v2 says: *"a cross-island call works with neither island having a `calls` row at all — Bob joins the channel room; Alice is there."* Alice is **not** there. Two reads settle it:
  - `room_for_channel()` = `_namespaced(channel_id)` = `f"{settings.gateway_id}:{channel_id}"`. Live values: imagineering `GATEWAY_ID=imagineering`, enspyr `GATEWAY_ID=enspyr`. **Different room names for the same channel.**
  - `LIVEKIT_URL` is `wss://livekit.imagineering.cc` vs `wss://livekit.enspyr.co`. **Different SFUs entirely** — not one room mis-named, two separate servers.

  So the room is *not* "computed identically from signed data by both sides"; it is computed by each **island**, and the two disagree. Cross-island calling is broken today and neither round 1 nor v2 fixes it. This does not resurrect round 1 (it was worse), but it deletes the strongest argument for the demotion, and I asserted it without checking the derivation — the exact illegal move I'm seated to catch.

- **M2 — "Involuntary end" is already solved client-side, so the last server-only fact evaporates.** *(class: the machinery a rethink deletes)* `FOLD-v2.md` S1 justified the whole remaining table on two facts: *is one live now*, and *did one end involuntarily*. But the shipped app carries `kCallRingDuration = Duration(seconds: 30)` — *"How long a ring rings once admitted… After this it stops ringing."* A caller whose app crashes therefore rings the callee for **at most 30 seconds** with no server involvement whatsoever. There is no ring-forever bug for the island to fix. Increment 0 makes the stop *prompt*; the 30s ceiling makes it *bounded*. Two independent mechanisms, both app-side, both already shipped or one PR away.

  Strike M2 against S1 and the table's justification is not weakened — it is **halved to a single soft UI hint**, which is precisely Carnot's disproportion argument arriving from a different direction.

- **M3 — I preserved objecthood as a word, not as a property.** *(class: wrong option-frame)* v2's own claim is "objecthood ≠ its own room." But after the demotion the object cannot: decide anything, survive its own end (deleted), federate, or separate call N from call N+1 at the SFU. A name with no authority, no persistence and no boundary is not an object; it is a cache key. If I am honest about what round 1's per-call room was *for*, it was the media-session boundary — and v2 abandons that while keeping the ticket's title.

- **M4 — Two rounds of sunk design is exactly when this framing gets laundered.** *(class: the assumption the enthusiasm smuggled in)* `CRUCIBLE.md` was written excited about derived rooms. That died in Fold. Round 1 died at Temper. v2's demotion is genuinely a better *shape*, and that is seductive: it feels like progress, so the question "should this exist?" goes unasked precisely where it is cheapest to answer. `FOLD-v2.md` S6 asked it and then declined to follow it — *"I am not proposing that, because #3159 asked for occupancy."* A ticket asking for occupancy is not evidence that occupancy is worth a table, a webhook, a public endpoint, and a per-island-keys prerequisite.

**What holds:**

- **The demotion diagnosis was right even though the conclusion was wrong.** Eleven of round 1's fourteen flaws did trace to one decision — putting a local best-effort row on the camera path. Naming that is real and it is what made the DISSOLVE visible.
- **`POST /v1/channels/{id}/video-token` must not move.** Nine rounds of cage-matched trust boundary; a moved gate is a rewritten gate. v2 got this right by reverting round 1's F14.
- **Signed messages are the correct durable substrate** for invite, end, and missed-call history. They federate and carry attribution; a local SQLite CDR does neither.
- **`live` as a label and never a gate** is a correct constraint and should survive into whatever ships.
- **Increment 0 is the whole product fix** and costs the island nothing.

**If RECAST, what to fold back:**

Not a RECAST. The concrete disposition:

- **Build nothing on the island.** Hand increment 0 to the app tab: a second signed sentinel over the door that already exists.
- **Correct the record**: cross-island calling is blocked by per-island SFUs and gateway-namespaced rooms, not by the absence of a call object. That is a separate, larger design (shared SFU or SFU federation) and it should be filed as such rather than left implied.
- **Re-file occupancy honestly** if it is still wanted: an advisory presence hint, priced against its own privacy and webhook cost — not as "a call as a first-class object."
