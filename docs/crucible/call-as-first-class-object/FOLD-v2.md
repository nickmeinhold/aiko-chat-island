# FOLD v2 — the author's own strike on `DESIGN-v2.md`

*Movement 4, round 2. Pre-adversary, no round budget. Findings marked **FOLDED** are already
applied to `DESIGN-v2.md`; findings marked **CARRIED** are stated in the design as accepted
costs for the Temper to strike.*

---

## S1 — The table does not earn its own history. **FOLDED — and it is the biggest cut.**

I claimed the row gives us *liveness + history + attribution*. Pressing on it: **history is
already in signed messages, and it is strictly better there.**

An invite is a durable signed row in permanent history. Increment 0's end-message is another.
A missed call is *an invite with no answer* — renderable entirely client-side, from data that
**federates and is cryptographically attributable**, which a local SQLite row is not. Round 1
put history in the row because the row was already there for arbitration. Once arbitration is
gone, history has no reason to follow it.

What genuinely cannot be done with signed messages alone:

| Need | Signed messages | Row |
|---|---|---|
| who started, when | ✅ authoritative + federates | ❌ island-asserted |
| missed call | ✅ invite with no answer | ❌ |
| voluntary end | ✅ increment 0 | ❌ |
| **is one live right now** | ❌ | ✅ |
| **involuntary end (caller's app crashed — nothing signed)** | ❌ | ✅ |

**Two rows in that table, not five.** So:

**The table is `live_calls`, and a row is DELETED when the call ends.** Not `ended_at`, not a
tombstone — deleted. `ended_reason` goes with it.

This is not cosmetic. It **dissolves most of F12** — Tesla's *"we accidentally incorporated a
phone company."* There is no call-detail record, because there is no retained record: a row
exists only while a call is believed live, on the operator's own box, for the two people
already in the DM. Retention policy becomes **zero by construction** rather than a number
someone must choose, implement a sweep for, and be trusted to honour. The account-deletion
cascade shrinks to deleting at most one ephemeral row.

*The residue I am not claiming away:* a live row still discloses "these two are on a call
right now" to whoever holds the box, for the duration. That is unavoidable for any island
that can answer "is a call live" at all, and it is the same disclosure the SFU already has.

## S2 — `live` must be a LABEL, never a GATE. **FOLDED.**

My claim-1 trace ("no camera action depends on the table") held for start, answer, re-join,
second device and reconnect — and **broke on the sixth path I had not traced**: tapping a call
event in history to join an ongoing call. There the app asks `GET /call` first, so a stale
`live: false` **removes the user's ability to join a call that is genuinely in progress.**

That is the capability-invariant failure exactly (`feedback_implementation_vs_capability_milestone`)
— the code path works and the human capability is gone.

**Fold:** joining is always permitted; the room is the same either way. `live` colours the
button, it never disables it. Stated as contract in the design, because it is precisely the
kind of thing an implementer "tidies" into a guard.

## S3 — The zombie participant is real, and it is the best argument against this design.
**CARRIED — stated as the counter-case.**

Per-call rooms give every call a clean slate. An eternal room accumulates ghosts: A's app
crashes without disconnecting, and A's stale participant sits in the room until LiveKit's
`departureTimeout`. B calls again ten minutes later and may meet a frozen tile of A.

I will not argue this away. Three honest points, and the Temper should weigh them itself:

1. It is **not a regression** — it is the behaviour of the shipped, live-verified
   implementation today. v2 declines to fix it; it does not introduce it.
2. It is **bounded** by `departureTimeout` — a number I still have not read off the box, which
   is why it stays an open variable rather than a mitigation.
3. Per-call rooms would fix it at the cost of the eleven flaws that killed round 1.

If a family judges the ghost worse than those eleven, that is a real argument and I want it
made explicitly rather than assumed away.

## S4 — Degenerate states, enumerated. **FOLDED where they bite.**

- **n=0, no call ever:** `GET /call` → `200 {live: false, call_id: null}`. It must **not** 404
  — 404 is reserved for existence-hiding ("you cannot see this channel"). Conflating the two
  makes "no call" indistinguishable from "no such channel". Folded as explicit contract.
- **Glare (two simultaneous `POST /calls`):** both see no live row, both insert, one hits the
  unique violation, catches, re-reads in a fresh transaction, returns the winner's `call_id`
  with `joined: true`. If the re-read finds nothing (winner already ended), retry once, then
  give up and return 503 — **and the app still places the call**, because `POST /calls` is
  advisory. Glare cannot strand anyone here; both parties computed the same room before either
  request was sent.
- **Webhook before the row exists** (a call so short `room_finished` beats `POST /calls`): the
  row is created after the room died and stays live until `max_duration`. Advisory, bounded,
  named.
- **Late `room_finished` after hangup-and-recall:** rejected by the `event_ts > started_at`
  ordering rule (F6). Kept from round 1 — it was right.
- **Takedown of the invite (#3163):** the retraction forward-event already exists (PR#104).
  Whether a takedown should also delete the live row is a **new open variable** this fold
  surfaced; I do not think it should (moderating a *message* should not silently kill a
  *live call* people are talking on), but it needs a decision, not a default.

## S5 — Constraint (c) is satisfied structurally, not by a rule. **Confirmed, not folded.**

Nick's constraint: *fail-open must not fall back to `room_for_channel`.* In v2 there is no
fallback because there is no second room — `room_for_channel` is the only room there has ever
been. A rule nobody can violate beats a rule someone must remember
(`concept_remove_coupling_not_guard_window`). Worth stating plainly so the Temper does not
read the constraint as unmet.

## S6 — I tried to dissolve my own problem and could not, quite.

The honest attempt: *if increment 0 stops the ring and the room is the channel's, does the
island need a table at all?*

Very nearly not. Everything except **live occupancy** and **involuntary end** is better served
by signed messages. If Nick would accept "a crashed caller's call shows live until the peer
gives up", the whole island half reduces to **zero code** and a handoff to the app tab.

I am not proposing that, because #3159 asked for occupancy and a call that never appears to
end is the reported bug's cousin. But the reduced table — two facts, deleted on end — is what
survives the attempt, and that is why S1 cut so deep. **The Temper should test whether even
that remainder earns its place.**
