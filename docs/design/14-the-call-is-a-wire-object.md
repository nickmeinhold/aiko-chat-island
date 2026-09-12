# Design 14 — the call is a WIRE object, not a channel property and not a database row

**Status:** candidate for a cross-family design temper. **Nothing here is decided.**
**Written:** 2026-09-12, from a conversation with Nick, in about twenty minutes. That age is
stated because the confidence in section 4 runs ahead of it.
**Answers:** Nick's product ruling of 2026-08-17 — *"but do calls ride DMs? that seems like a
bad design to me"* → **"gather people for a thing."** The ruling made `DM-only` scaffolding with
an expiry, and nothing has been built against it in the 26 days since.
**Bundle:** island 12, 12a, 12a-TEMPER, 12a-MEASURED, 12a-RECAST; app 16 v2 @ `6bd4ab2`
(PR #196, **open**), design 19, design 20, ADR-0004; claude-tasks #3170, #3171, #3196, #4265,
#4325, #4326.

---

## 0. What a call IS today, which is less than it looks

```python
CALL_INVITE_BODY = "aiko:call/1 · 📞 started a call"      # a literal message body
should_wake(kind, body) = kind == "dm" and body == that    # the entire wake decision
room = room_for_channel(channel.id)                        # the SFU room IS the channel
payload = {"c": channel_id, "k": "call_invite"|"call_end"} # the wake names the channel
```

**A call is a magic string spoken into a channel.** There is no call object — not in the
database, not on the wire, not in the room name. "This call" and "the next call" are the same
SFU room forever.

**That single absence is the whole of this document's case**, because it explains an entire
backlog at once:

| open item | what it actually is |
|---|---|
| #4265 "the wake names a channel, not a call" | there is no call to name |
| temper flaw 4, three clocks unowned | no object owns a lifetime, so every timer is a free constant |
| temper flaw 10, per-call state | "per-call state" has nothing to attach to |
| temper flaw 7, UUID contract "by construction" | nothing to check an id against |
| #3159 occupancy is channel occupancy | because the room is the channel |
| DM-only | because the pair IS the channel |
| #4325 lone `call_end` wakes | an end cannot be matched to an invite |

**They are not seven findings. They are one absence seen from seven angles.** Any document that
fixes them one at a time is patching shadows.

---

## 1. The claim

> **A call should be an object that exists ON THE WIRE — carried by a signed invite — and
> nowhere else. Not a property of a channel, and not a row in the island's database.**

Three pieces:

1. **Identity.** The client already mints a ULID for CallKit's UUID (design 12). The id exists;
   the island simply never sees it. This is not inventing an identity, it is **promoting** one.
2. **Room keying.** `room_for_call(ulid)` replaces `room_for_channel(channel.id)`. This one
   change buys cardinality (this call vs the next), ephemerality (rooms die), and decoupling
   from the pair. It requires the v2 invite to carry the ULID — #3171, with an agreed shape
   (`aiko:call/2 <ulid> · 📞 started a call`) that currently **exists only in a chat
   transcript**.
3. **Participant set.** Named in the signed invite. Filtered by island policy. Never stored.

---

## 2. Authorization is THREE questions currently fused into one predicate

`should_wake(channel_kind, body)` answers all of these at once today, by accident of the
channel model. They separate cleanly and they have different homes:

| question | shape | home | stateful? |
|---|---|---|---|
| may A **wake** B? | per-edge | island policy (block, conduct), read **fresh at send time** | no |
| may B and C be **co-present**? | per-**set** (induced subgraph) | island policy over the proposed set | no |
| who **is** in this call? | per-room | the SFU, read via occupancy (#3159) | no |

### 2a. The co-presence row is the one that is easy to miss

**Per-edge authorization of INVITATION does not authorize CO-PRESENCE.** A invites B and C;
edge A→B passes; edge A→C passes; **B and C have blocked each other.** A is now a vector for
putting two people in a room who refused each other.

This is not a new problem and **the codebase already knows it** — it is the stated reason for
DM-only: *"a group/public room can't enforce pairwise BLOCKS at a room-level token (unbounded
participants, all-or-nothing subscription)."* DM-only is the current answer, and it works
because a DM room has exactly one pair. Any gathering model owes a new answer, and
`n(n−1)/2` pairs is a categorically different check from one.

Consequence worth stating: **occupancy stops being a convenience and becomes an authorization
input**, because the set can change after the invite. That is precisely what the 2026-08-17
ruling meant by *"occupancy of a gathering is a different question from occupancy of a channel
room."* PR #167 is load-bearing under this design in a way it is not under the current one.

### 2b. So the ACL is a FUNCTION, not a table

> `may_be_in(call) = f(signed invite's participant set, current island policy, live occupancy)`

Nothing in that is stored. The set arrives on the wire, the policy is already in tables the
island keeps for other reasons, occupancy belongs to the SFU.

**This is the answer to temper flaw 10's gate** (*"either show a stateless construction, or name
the state and price it against the DISSOLVE's reasoning"*). It is also why this is not the row
#3170 dissolved: that row was **decorative** under `room = channel.id`. Under
`room = call.ulid` the identity is load-bearing at the SFU, and it still needs no row.

### 2c. Revocation still works, and that is not an accident

The invite carries **identity**; the island supplies **policy**, re-read at every gate. So a
ban, a block, or a conduct-gate change between invite and join is honoured, because the island
never trusted the invite for authorization — only for identity. This is the property that
disqualified temper arm (C) (*"a minted capability sits in the caller's hands and cannot be
revoked, only expired"*), and the separation is what avoids inheriting it.

**Do not confuse this with arm (C).** That was a *blind-signed ring capability*, minted by the
recipient, hiding the pair from the island, authorizing a **ring**. This is a *readable signed
invite*, minted by the caller, hiding nothing, authorizing **room entry**. Different object,
different purpose, different failure modes. (The author conflated exactly these two on
2026-09-12 and it took Nick one question to surface it.)

---

## 3. The three clocks become derivations, because something finally owns a lifetime

Temper flaw 4 demanded *"one invariant table with derivations, not constants"*. It could not be
written, and this document's claim is that it could not be written **because no object owned a
lifetime**. Give the invite an `expires_at`:

| quantity | today | under this design |
|---|---|---|
| APNs expiration | 60s constant | **≤ `invite.expires_at`** — never deliver a wake for an invite already dead |
| ring ceiling | 30s constant, owner disputed | **IS `invite.expires_at`** — the entry window and the ring are one quantity |
| client freshness | 10s constant | **derived** — a wake is fresh iff its invite has not expired |
| room lifetime | SFU `empty_timeout`, 300s | **unchanged, and correctly separate** — the invite bounds ENTRY, not duration |

The fourth row is the one that keeps this honest: the invite's expiry is **not** the call's
length. A ring stops at 30s; a call may run an hour. Collapsing those would be the same
over-reach the temper's M4 amendment already caught once.

**Nick's ring-ceiling ruling survives intact**: the island still owns the ceiling, because the
island **sets** `expires_at`. The device **enforces** it locally and voids it on answer — which
also removes temper flaw 3's sharpest hazard (*"it can murder a live conversation: the island
cannot know the callee answered at T+8s, and at T+30s the stop fires"*), because the party that
knows the call was answered is the party enforcing the deadline.

---

## 4. The move with the most reach, and the least age

> **The invite stops being a channel message.**

Today the invite is a **permanent signed message in channel history** — the code says so:
*"inside signatures already sent to both live islands and stored in permanent history, so it
can be added to but never edited."*

A call is ephemeral. We are recording an ephemeral gathering as an immutable entry in a
permanent record, and **that permanence was never chosen** — it is inherited from the transport.
The invite rides messages because in 2026-08 that was the only path that reached a device.

**That is no longer true.** VoIP shipped in v0.11.0; the island wakes handsets directly. If the
wake carries the signed invite, the channel message is redundant — except as a "missed call"
record, which is a **product** choice, and Nick has already ruled *no refused-ring record* for
the blocked case (2026-09-01).

So "stop riding DMs" may most precisely mean: **the invite is a first-class wire object, not a
message that happens to say a magic phrase.**

**THIS SECTION IS THE LEAST TRUSTWORTHY THING IN THIS DOCUMENT and is flagged as such.** It is
twenty minutes old, it changes a signed wire format live on two islands, and its author's
confidence in it is running ahead of its age. It is the part most worth attacking.

Known costs, stated rather than discovered:
- The live-socket path needs a new event type; the invite currently reaches an open app as an
  ordinary message.
- Anything that reads history to reconstruct call activity stops working.
- Two delivery paths (socket, wake) must agree on one object.

---

## 5. What this does NOT buy

Written explicitly because the author reached for the first of these twice in ninety minutes,
the second time immediately after naming the pattern.

- **It does not create the "third cell"** — silence for a stranger without the island knowing
  the pair. For the island to decide ring-vs-alert for a stranger it needs *B's policy about A*,
  which is inherently standing and pair-shaped whoever holds it. This design changes whether the
  fact must be **retained** (it can ride the wire per-call) — **not whether it must exist.** The
  two-cell result of `12a-RECAST.md` stands.
- **It does not buy anonymity.** The island routes the wake, therefore it holds the device
  token, therefore it knows who it woke. Unchanged.
- **It does not settle cross-island gatherings.** Nick's 2026-08-30 callee-hosts ruling is
  explicitly scoped to 1:1 and refuses to generalise; a gathering across 3+ islands has no
  single callee. Still #3196.
- **It does not deliver group calls.** Media for multi-party still needs selective subscription
  (#2731), which is a hard prerequisite the 2026-08-17 ruling already promoted.

---

## 6. Costs and hazards

1. **The v2 invite is a one-way door.** `is_call_invite` is **exact equality**, so shipping a v2
   invite before the island has a v2 read path **silently stops every ring on both islands, with
   no error anywhere**. Island-first, with a compatibility branch.
2. **The co-presence check is a new trust surface.** Cage-match by law.
3. **Invite-as-bearer needs replay discipline.** A signed invite is valid until it expires;
   `expires_at` is the only bound, and the island must refuse mints past it.
4. **Two-path agreement.** Socket and wake must carry the same object, and a divergence is the
   silent-desync class this repo has been bitten by before.
5. **It reopens a dissolved design.** #3170 closed 3/4 DISSOLVE. The bar for reopening was *"name
   a server-side decision that must be authoritative"*; §2a's co-presence check is offered as
   that decision. **A temper should test whether it really is, or whether it is the same
   decorative object wearing a function's clothes.**

---

## 7. Questions for the strike

1. Is §2a's co-presence check genuinely an authoritative server-side decision, or does it
   collapse back into something derivable — i.e. does this reopen #3170 on a false premise?
2. Does §4 survive contact with the live-socket path, or does "not a message" simply relocate
   the permanence rather than remove it?
3. Is `invite.expires_at` as the single lifetime source actually stateless, or does enforcing it
   require the island to remember which invites it issued — which is the row again?
4. §3 claims the device voids the ceiling on answer. What happens when the device is wrong,
   lying, or an older build?
5. Does anything here regress the properties `12a-RECAST.md` records as holding — in particular
   the two-cell result, and the closure of arm (C)?

## 8. Provenance

Nick raised the premise unprompted on 2026-08-17 (*"but do calls ride DMs?"*) and again on
2026-09-12 (*"I still reckon we should not ride DMs for calls... but we settled that didn't
we?"*). **It had been settled, in his favour, and the code went the other way anyway** — this
document exists because the second asking found the first ruling unimplemented 26 days later,
with PR #167, #4265 and the RECAST's own structural result all built deeper into the model the
ruling retired.
