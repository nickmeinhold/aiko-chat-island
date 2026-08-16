# Design v2 — a call as a first-class object (island half)

*Round 2 of ≤3. Recast after `TEMPER.md` returned **4/4 RECAST**, 0 DISSOLVE, 14 fatal flaws.
Status: **UN-TEMPERED** — this recast has not been struck. Do not build on it yet.*

---

## What changed before a word of this was written

Round 1 was cast against the ticket and my own memory. The mandated cross-tab grounding
(`CLAUDE.md`, the #2634 lesson) was run properly this time, and it moved the ground:

**The app tab shipped the ring at 10:36 this morning** — `aiko_chat_app@263f33f`,
*"feat(call): the ring — a call invitation you can't forge (#2808)"*, six-round cage-match,
19 findings, and:

> LIVE-VERIFIED end to end against the real enspyr island with a second identity: signed
> invite → ring banner on an unrelated route → Answer → LiveKit room joined, camera
> publishing over forced-relay TURN.

Three facts follow, and each one bears directly on this design:

1. **`room = channel_id` is live in a shipped, verified client.** `CallInvite.channelId` is
   documented as *"The LiveKit room to join. The room IS the channel id (#2726)."*
2. **The app already wrote the migration contract for this design** — #3171, *"TRIGGER
   (island #3170 lands): invite wire v2 — carry call_id, keep a v1 read path forever."*
   The app is not blocking; it is waiting, with the v1 compatibility branch pre-committed.
3. **A call now completes end to end.** Not two humans yet — one human, two identities —
   but the product path from signed invite to publishing camera is no longer hypothetical.
   Round 1 was designed as though nothing worked. Something works, and this design is now
   a *change to a live path*, not a greenfield build.

---

## The recast, in one line

**Round 1 made the call row the ARBITER — the thing that decides which room you enter.
v2 demotes it to a RECORD — the thing that remembers a call happened. The room stays
derived from the channel.**

That single demotion dissolves nine of the fourteen fatal flaws outright and shrinks three
more, because almost every one of them was a consequence of putting a local, non-federating,
best-effort-maintained SQLite row on the path that lights a camera.

### Why this is still "a call as a first-class object"

#3170's headline is *"a call should be a first-class object with its own room
(`call:<ulid>`)"*. **v2 keeps the objecthood and drops the room.** A call gets an identity, a
lifecycle, a start, an end, an author, and a history row — everything that makes it an object
you can name and close. What it does not get is *its own LiveKit room*, because the room is
the one property that must be computed **identically, offline, by both parties, from signed
data** — and a row in one island's SQLite cannot be that.

**This partially reverses the ticket, and I am surfacing it rather than tie-breaking it.**
#3170 is an app-tab-filed issue against this repo; Temper should strike this reversal
directly. My position: objecthood was the real want, the room was the assumed mechanism, and
the mechanism is what round 1 died on.

---

## The shape

**A call is a row. The room is the channel's, as it already is. The row never decides where
a camera points.**

The room continues to be `room_for_channel(channel.id)` — the existing namespaced derivation
in `domain/livekit_tokens.py`, unchanged, still opaque to the client, still only ever
delivered on the token response.

### The invariant that replaces round 1's arbitration

> **No camera action depends on reading the `calls` table.**

Joining is: signed invite → `channelId` (inside the signature) → existing
`POST /v1/channels/{id}/video-token` → opaque `room` → join. That is the path that is live
and verified today, and v2 does not touch a single gate on it.

The `calls` row answers three questions that path cannot: *is one live right now?* (#3159
occupancy), *did one happen?* (history, missed calls, #3164), and *who started it?*

### Data — two facts, and the row is DELETED when the call ends

`FOLD-v2.md` S1 cut this table roughly in half. History is **already in signed messages** and
is strictly better there — an invite is a durable, federating, cryptographically attributable
row, and a missed call is *an invite with no answer*, renderable client-side. Only two things
genuinely cannot be answered from signed history: **is a call live right now**, and **did one
end involuntarily** (the caller's app crashed, so no hangup was ever signed).

So the table holds exactly those, and a row is **deleted** on end — not tombstoned:

```
live_calls
  id            TEXT PK       -- ULID, server-minted
  channel_id    TEXT NOT NULL UNIQUE   -- one live call per channel; de-dup, NOT a mutex
  started_by    TEXT NOT NULL -- users.id; ISLAND-ASSERTED, not federated-signed
  started_at    TEXT NOT NULL -- server-observed receipt time, never client signedAtMs (F7)
  invite_msg_id TEXT NULL     -- the signed invite that announced it, when there is one
```

**There is no `ended_at` and no retained call record.** This is not cosmetic: it dissolves
most of F12 (*"we accidentally incorporated a phone company"*). Retention becomes **zero by
construction** rather than a number someone must choose, sweep for, and be trusted to honour,
and the account-deletion cascade shrinks to at most one ephemeral row.

The unique constraint survives with a completely changed meaning. In round 1 it was a mutex:
losing it meant you could not start a call. Here it de-duplicates a *record*: losing it means
your call is recorded as a join to the existing row. **A stale row can no longer prevent a
call**, because starting one never consults it — which is exactly why F3's permanent channel
lock cannot occur.

### Endpoints

```
POST /v1/channels/{channel_id}/calls   -> 200 {call_id, joined: bool}     (record a start)
GET  /v1/channels/{channel_id}/call    -> 200 {live, call_id, participants, started_at}
```

- **`POST /v1/channels/{id}/video-token` DOES NOT MOVE and DOES NOT CHANGE.** This is a
  deliberate reversal of round 1 (F14). That route carries a nine-round cage-matched trust
  boundary — existence-hiding 404, DM-only + `is_private`, 2-party cardinality assertion from
  raw `Membership` rows, `is_blocked_between`, `is_posting_member` → `can_publish`,
  rate-limit, `no-store`, 503-when-unconfigured. A moved gate is a rewritten gate. It stays.
- **`POST /calls` returns `call_id` only — never `room`** (F13). One source for the room name,
  forever: the token response.
- **`POST /calls` is advisory.** Its failure (404, 503, timeout, 500) **must not block a
  call.** The app proceeds to `video-token` regardless. This is the fail-open that round 1
  could not safely have and v2 can — because failing open now means *"the call is
  unrecorded"*, not *"the two parties are in different rooms"*.
- **`GET /call` keeps #3159's contract exactly**: membership-enforced, existence-hiding 404
  not 403, 503 when video is unconfigured, `participants` is a **count only**, `no-store`,
  and it is **never a ring trigger** (F12) — the ring comes from the signed invite alone.
- **`live` is a LABEL, never a GATE** (`FOLD-v2.md` S2). Joining is *always* permitted — the
  room is the same whether or not a row exists, so `live` colours the button and never
  disables it. A stale `live: false` must not be able to remove a user's ability to join a
  call that is genuinely in progress. This is stated as contract precisely because it is the
  kind of thing an implementer tidies into a guard.
- **"No call" and "no such channel" are different answers.** `GET /call` with no live row is
  `200 {live: false, call_id: null}`; **404 is reserved for existence-hiding.** Collapsing
  them makes an empty channel indistinguishable from an invisible one.
- **DM-only is gated on the mutator** (F10): `POST /calls` re-applies the same
  `kind == 'dm' AND is_private` + 2-party + block checks as `video-token`. Backend-first,
  one door.

### Increment 0 is not ours, and it dissolves the reported bug

F8 was right, and grounding sharpens it: the reported failure — *caller hangs up, callee
keeps ringing* — is fixed by **a second signed message over the door that already exists**.
No table, no webhook, no TTL, no outbound SFU call, and **zero island change**: the island
already relays signed messages.

That makes increment 0 **app-tab work** (it owns `kCallInviteBody` and every sentinel string;
a new sentinel is a one-way door into signed history and Nick confirmed the last one by hand).
This design does not claim it. It is a handoff, and it should ship first — it is strictly
cheaper than everything below and it is what actually stops a ring.

The `calls` table is then scoped to exactly what a signed message cannot do: **an involuntary
end** (the caller's app crashed, so no hangup was ever signed) and **history**.

### Liveness, without a mutex to defend

The row is **deleted** by whichever comes first:

- **hangup** — the island observes the increment-0 signed end-message (or the app calls an
  explicit end endpoint). Cheap, common case, and the only writer that runs on a healthy call.
- **empty_reap** — a webhook says the room went empty, *and* the event's timestamp is after
  `started_at` (F6: a late `room_finished` can never close a call that started after it).
  Short.
- **max_duration** — a long safety ceiling, unrelated to `empty_reap` (F4: two numbers, never
  one). Neither is on a camera path, so a mis-set value costs occupancy accuracy, not a call.

**Nothing here is authoritative and nothing needs to be.** A stale row shows a wrong occupancy
dot. It cannot strand anyone, lock a channel, or light a camera.

### The counter-case I am not arguing away (`FOLD-v2.md` S3)

Per-call rooms give every call a clean slate; an eternal room accumulates ghosts. If A's app
crashes without disconnecting, A's stale participant sits in the room until LiveKit's
`departureTimeout`, and B's next call may open onto a frozen tile of A.

Three honest points, for the Temper to weigh rather than for me to settle:

1. It is **not a regression** — it is the behaviour of the shipped, live-verified
   implementation today. v2 declines to fix it; it does not introduce it.
2. It is **bounded** by `departureTimeout` — a number still unread off the box, which is why
   it stays an open variable rather than a mitigation.
3. Per-call rooms would fix it at the cost of the eleven flaws that killed round 1.

If a family judges the ghost worse than those eleven, that argument should be made explicitly.

---

## Federation — the flaw that killed round 1, now load-bearing in our favour

Tesla's F2 was the sharpest strike: *"A call that spans two islands is not an edge case. It
is the product."* Round 1's answer was to scope the increment to same-island DMs and forbid
fail-open — a retreat.

v2 does not need the retreat. Both parties derive the same room from the same signed
`channelId` with no island round-trip, so **a cross-island call works with neither island
having a `calls` row at all.** Bob's island returns 404 on `GET /call`; Bob's app rings from
the signed invite; Bob joins the channel room; Alice is there.

Per Nick's constraint the increment is **still scoped to same-island DMs in writing** — the
block and cardinality checks that make video safe are same-island facts, and widening them is
#2731's work. But that scope is now a *policy* choice about who may be called, not a
*structural* limit hiding a two-room failure. **`room_for_channel` is not a fallback here; it
is the only room. There is no second room to fall back to, so constraint (c) is satisfied by
construction rather than by a rule someone must remember.**

## Forced release (constraint (d))

Round 1's arbiter could not die. v2's record has three independent release paths, and a
fourth that is not needed:

1. explicit hangup (app-driven, common case),
2. `empty_reap` on a timestamp-ordered webhook,
3. `max_duration` ceiling,
4. **and if all three fail, nothing is held** — the next call proceeds normally and is
   recorded against the stale row (wrong history, no lost capability).

A liveness bit with no forced-release path is a mutex you must remember forever
(`concept_remove_coupling_not_guard_window`). v2's answer is to remove the coupling: the bit
governs no capability, so its release is a data-quality concern rather than a deadlock.

## Blast radius & consent spine

- **#2732 (per-island LiveKit API keys) remains a HARD PREREQUISITE for the webhook
  receiver** (constraint (a), F5, 3 families). Until it lands, no webhook may write
  `ended_at` — advisory counts only. *Honest note for the Temper:* the demotion shrinks this
  blast radius substantially. With a shared HS256 secret a forged webhook now corrupts a
  record; in round 1 it was a remote "end anyone's call" primitive. I am keeping the
  prerequisite as instructed, but I do not want the reasoning to silently inherit a severity
  that no longer applies — strike this if you think I am wrong.
- **New inbound public surface** (the webhook) on a live island: JWT + payload-hash verified,
  rate-limited, fail-closed on an unverifiable signature.
- **`GET /call` is a presence surface.** Membership-gated, count-only, existence-hiding,
  `no-store`, never a ring trigger.
- **F12 — the CDR problem is mostly dissolved by S1, and the residue is named.** Deleting the
  row on end means there is **no call-detail record**: no durations, no history of who called
  whom, nothing to export. What remains is that a live row discloses *"these two are on a call
  right now"* to whoever holds the box, for the duration of the call. That is unavoidable for
  any island that can answer "is a call live" at all, and it is the same disclosure the SFU
  already has. Still required in this increment: the **account-deletion cascade** (FK-off with
  application-level cascades, so explicit code in `accounts_service`, not a constraint — now
  trivial, at most one ephemeral row) and `started_by` documented as **island-asserted, not
  federated-signed**.
- **Two authors for one fact** (F12, unfolded from round 1): the glare loser still says *"I
  started a call"* in signed history while the row credits the winner. Contract: signed
  history is authoritative for *who announced*; the row is authoritative for *what the island
  observed*. Missed-call rendering reads signed history, never the row alone.

## Claims to falsify (strike these hardest)

1. **"No camera action depends on the `calls` table."** Is that true of every path — answer,
   re-join, a second device, a reconnect mid-call — or only the happy path I traced?
2. **"Demoting the row makes fail-open safe."** Round 1's fail-open was fatal. I claim v2's
   is not, *because there is only one room*. Is there any state where an island's row and the
   derived room disagree about where a participant should be?
3. **"One room per channel is sufficient for a DM."** Two calls in one DM cannot be
   distinguished at the SFU. A participant who never disconnected from call 1 is present in
   call 2. Is that a phone line (fine) or a bug (not fine)?
4. **"The reversal of #3170 is correct."** I am overriding a shipped ticket's stated shape on
   the argument that objecthood ≠ its own room. Attack that directly.
5. **"Increment 0 is app-side and needs nothing from us."** Does the island truly need no
   change to relay and persist a second sentinel — no gate, no dedup, no takedown interaction
   (#3163)?
6. **"The unique index is now harmless."** It is still a database constraint that can reject
   a write. What breaks if the de-dup insert fails at an unexpected moment?

## Rejected alternatives

- **Round 1's own shape (per-call room, row as arbiter).** Rejected by its own Temper; the
  fourteen flaws are not individually patchable because eleven of them trace to one decision.
- **Derived room name** (`call:H(signer‖channel‖clientMsgId)`). Still rejected, still for
  glare: *a derived identifier structurally cannot dedupe.* Note what v2 does instead — it
  does not try to dedupe at all, because `room_for_channel` is **stable rather than derived
  per-invite**, so two simultaneous callers compute the same room and converge with no
  arbiter. Glare is not solved; it is **made impossible to express**.
- **Keeping the invite body unchanged forever.** v2 does not need wire v2 for the room, so
  #3171's trigger does not fire. A future v2 invite carrying `call_id` remains *available*
  for tighter history attribution — it is no longer a prerequisite for anything.
- **#3159 as originally cast** (occupancy on the eternal room, 1/s poll, outbound
  `ListParticipants` on the ring path). v2 keeps #3159's endpoint and its count-only contract
  but serves it from island state.

## Open variables (enumerated, not silently TODO'd)

- `empty_reap` and `max_duration` values. `empty_reap` depends on LiveKit's
  `departureTimeout` in our live v1.13.5 config — **read it off the box, do not guess.**
  Still unread as of this writing, and it also bounds the zombie-participant window above.
- **Should a takedown of the invite delete the live row?** (#3163, surfaced by `FOLD-v2.md`
  S4.) My position is **no** — moderating a *message* should not silently kill a *live call*
  people are currently talking on — but that is a decision, not a default.
- Whether the webhook receiver lives on the island or the media companion stack.
- Whether an explicit end endpoint is needed at all, or whether the increment-0 signed
  end-message is a sufficient hangup signal for the island to observe.
- **Whether the remainder earns its place at all** (`FOLD-v2.md` S6). Strip occupancy and
  involuntary-end, and the island half is **zero code** plus a handoff to the app tab. I am
  not proposing that — #3159 asked for occupancy, and a call that never appears to end is the
  reported bug's cousin — but the Temper should test the remainder rather than assume it.
