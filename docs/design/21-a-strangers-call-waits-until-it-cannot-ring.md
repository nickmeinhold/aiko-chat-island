# Design 21: a stranger's call waits until it can't ring

**Status: v3, BUILT, cage-match round 2 pending.** v1 was written 2026-10-29 before
any code. v2 folded in the app tab's answers to the three questions v1 asked (same
day, read from its main); answer 1 corrected v1's model of the residual and supplied
the fence cap. v3 folds round 1 of the PR's cage-match (#445: 3/3 REQUEST_CHANGES, ten
findings, eight real), most importantly that the hold is decided **inside the statement
that reads the row** (below).
It answers the island half of the app tab's design 25 temper (aiko_chat_app#444,
`docs/design/25-TEMPER.md`, Disposition), which moved this fix here from the app.

**The decision this builds (Nick, 2026-10-29 14:16):** *"withhold, and keep the missed
call."* The island holds a stranger's call invitation until it's too old to ring, then
delivers it. Every app version already draws a call invitation as a centred "X started
a call · 14:05" line, and none rings one past its freshness window. So the call lands
as a missed call on every installed app, with no app release and no new wire field.

## Why the island, and why now

enspyr is open to sign-ups (v0.20.0). Today any account can open a DM with any user
id and ring that user's **open** app. The wake gate (`push_service._spoken_here`)
already judges this caller a stranger and refuses the push, but its docstring says
outright: *"IT GATES THE WAKE, NEVER THE INVITE."* The same call frame still reaches
the open app over its socket, and the app rings it.

The deciding fact from the temper: **only withholding protects apps already
installed.** A stamp would be ignored, because old apps skip unknown fields. Any
app-side check reaches users only through an app release, and aiko has no release route
yet. The signed body can't be rewritten in transit, so the island has exactly two moves:
deliver or withhold.

## The rule

A message `I` is **held from viewer `R`** while all of these are true:

1. `I` is a call/2 **invite** (`push_service.parse_call_body`, the one parser), not an END;
2. in a **DM** channel (`ChannelKind.DM`), the only place the app rings;
3. from a **person**: `I.sender_kind == human`. A non-person (an agent account) needs
   per-conversation consent on the handset, which a stranger can't have. 25-TEMPER,
   "What holds": *consent is the question for non-persons, conduct for persons.* Holding
   agents too would break a consented agent ringing an open app, which works today;
4. `R` is not the sender, and **`R` had not posted in that DM before `I`**: no message
   row from `R` in the channel with `id < I.id`. Soft-deleted rows count, as they do in
   `_spoken_here` today ("soft-delete is not withdrawal");
5. **now < `release_at(I)`**.

Condition 4 is decided by **message order, not by time of evaluation**, so the hold can
be re-derived from rows that already exist. No row records who was held, and the
caller isn't told (the no-refused-ring-record ruling, Nick 2026-09-01). If `R` replies
during the hold, that doesn't release `I`. `R`'s reply lets the *next* call through,
and this call stays a missed call.

**The wake gate moves onto the same definition.** `_spoken_here` today asks "has `R`
ever posted here?" at the moment the wake runs, which is a background task a few
milliseconds after the write. If `R` posts inside that gap, the wake would ring a
handset whose history then holds the invite. Both gates now ask condition 4 with the
invite's own id as the bound, so they cannot disagree.

## The clock: when is it too old to ring?

The app rings an invitation (with no recorded wake, which a held one never has) only if
`−5 s ≤ app_now − signedAt ≤ 10 s` (`call_invite.dart`, `kCallInviteClockSkewTolerance`
and `kCallInviteFreshness`). Both are judged on the **recipient's** clock against the
**caller's** signature. The island controls neither clock.

    release_at(I) = max(signed_at_ms(I), received(I)) + HOLD,   HOLD = 60 s

- **Anchored on the signature**, because that's what the app measures. Taking the max
  with the island's receipt covers an invite that has no `origin`, and costs nothing when
  the caller's clock is slow.
- **HOLD = 60 s** clears the app's 10 s freshness with 50 s to spare for the
  recipient's clock running behind the island's. It also exceeds the 30 s ring ceiling
  (`push_result.RING_CEILING_SECONDS`), so the missed call appears after any ring for it
  would have ended. This is a fifth call clock; reconciling the clocks is
  claude-tasks#4233, and this doesn't try to.
- **A future-dated invite is refused at ingest.** In a DM, a call/2 invite whose
  `signed_at_ms` is more than **F = 60 s** ahead of the island's clock gets the existing
  `bad_origin` error. That is the existing code with a new trigger, and no new wire
  vocabulary. Two reasons:
  - Without the cap, a stranger could sign an invite a day ahead. Released early, it
    would ring whenever the recipient's app re-read history near the signed time. Held
    until then, it would sit outside any bounded scan.
  - The refusal applies to **every** caller, friend or stranger, so it reveals nothing
    about the recipient's conduct. An honest caller whose clock is a minute fast already
    fails every ring (the app refuses it as `clockSkew`); now they get an error instead.
- **The scan is a primary-key range.** A ULID's first 48 bits are its arrival time. So
  with the cap in place, every invite that could still be held satisfies
  `id > ulid_floor(now − HOLD − F)`: two minutes of rows, using the index the table
  already has.

## The doors

Built by grepping every caller of `message_view`, `message_frame`, `get_history`,
`latest_ulid(s)`, `last_visible_message(s)` and `visible_message`, not from memory.

| Door | Code | Can ring the app? | What the hold does |
|---|---|---|---|
| WS send fanout | `ws._handle_send` → `hub.fanout` | **yes** | held recipients join `exclude_user_ids` |
| WS bus-ingest fanout | `main._ingest` → `hub.fanout` | no (a DM never rides the bus; `persist_inbound` drops `dm:`) | same exclusion function, one door |
| REST history | `rest/messages.history` → `get_history` | **yes** | shared visibility predicate |
| Subscribe fence | `ws._handle_subscribe` → `subscribe_view` | no rows, but history must agree with it | shared predicate, and **capped below a held invite** (below) |
| Read-position bound | `read_positions_service` → `latest_ulids` | no | shared visibility predicate |
| DM list preview | `rest/dm.list_dms` → `last_visible_messages` | no | shared visibility predicate |
| Fetch by id | `rest/messages.get_message` → `visible_message` | no | 404 while held |
| Reactions | `rest/reactions` → `visible_message` | no | 404 while held |
| Wake (push) | `push_service.wake_for_message` | rings a **locked** phone | already gated; moves to condition 4's bound |
| Subscribe replay | **new**: only when the fence is capped | yes (live frames) | replays the visible rows above the cap; never the held one |

The app tab read its own main on 2026-10-29 14:22: **only two of these can ring the app**,
WS message frames and REST history. The others stay on the list for visibility to other
clients, and for federation later.

**One predicate.** `get_history`, `latest_ulids` and both `last_visible_message`
variants already share `moderation_service.not_blocked_predicate`, and the fence's
docstring explains why they must: the reconnect pager runs until `cursor >= fence`.
The hold becomes part of that shared predicate (`messages_service._visible`), and
`visible_message` reads through the same predicate in one statement. **Visibility only
grows over time under the hold** (held → released, never the reverse), so the
documented shrink race between fence and pager gets no new case.

**The statement that returns a row decides whether it is held (v3).** v2 computed
the held set in one SELECT and applied it as `NOT IN (held)` in the next. Under
pysqlite each SELECT is its own read, so an invite that committed between the two
was missing from the set and present in the rows. REST history, a door that rings
the app, served it fresh (Carnot, round 1; measured red by injecting the commit
between statements). This repo's rule for writes ("fold the predicate into the
write") holds for a read that *is* the boundary. So the hold is one SQL clause,
`call_hold.held_clause`, correlated on the row being read:
- the body matches `INVITE_GLOB`, a case-sensitive whole-string GLOB derived from
  `CALL_ID_PATTERN` and pinned against `parse_call_body` by a test corpus;
- conduct is `push_service.prior_post`, the same SQL fragment the wake gate asks;
- the time is pure string comparison on the ULID id. `receipt + HOLD > now` is
  `id >= ulid_floor(now − HOLD + 1)`, and `signed + HOLD > now` reads the JSON field.
  Nothing is decoded, so nothing can raise on a malformed id.

The send exclusion (`held_viewers`) and the sweeper (`due_releases`) ask the same
clause, so there is one definition of "held", not a SQL one and a Python one.

**The clock never runs backwards (v3).** Expiry is an absolute instant. A backward
step of the wall clock (an NTP correction) would re-hold a released invite, which
would *shrink* visibility and hang a pager whose fence already names it (Tesla, round
1). `call_hold.now_ms` is a process-wide high-water mark of wall time.

## Delivering it late (the missed call)

History alone isn't enough. A connected client treats every id above its subscribe
fence as live and pages only on (re)subscribe, so a released invite would wait unseen
until the next reconnect. The app tab voted for a **delayed live send**: a late frame
with an older id slots into place by id order, and the app refuses to ring it as
`stale`. (What keeps a *reconnect* from stranding it is the fence cap, in the next
section.)

**A release sweeper, not a timer per invite.** One background task runs every 2 s. It
finds invites held from someone at the last tick and not at this one (the same
`held_clause` at two instants) and sends each recipient their own view, reactions
included, on their connections subscribed to that channel. A block made during the
hold wins. It returns, and logs, the number of **connections** reached, so "nobody was
listening" never reads as a delivery (Noether, round 1). A failing tick retries its
window, clamped to five minutes so it can't grow without bound.
- **Restart-safe by construction.** Its input is the database and the clock, so a hold
  that spans a restart is found again; there are no timers to lose. On boot it starts at
  `now`, because no socket is connected yet to receive what fell due while the island
  was down. Those clients reconnect and read history, where the invite is now visible.
- **A duplicate is harmless.** The app's cache is UNIQUE on the server id
  (`_handle_subscribe`'s docstring relies on the same fact), and a duplicate is stale
  anyway.
- **Hub change:** `fanout(..., only_user_ids=...)`, so the release reaches the recipient
  and not the caller, who already has it.

## The subscribe fence is capped below a held invite (v2)

v1 named a residual: a device whose watermark passed the held invite would page forward
from above it forever and never show the missed call. It got the mechanism wrong. The
app tab read its code (2026-10-29): **a live frame never moves the watermark.** Only the
forward pager writes it (`advanceHistoryContiguous`), and only up to the subscribe
fence. Nothing ever reads the newest page backwards (`getHistory` has one caller, the
forward pager). So the real path is a **reconnect during the hold**: the new fence
counts a later message L but not the held invite I, the pager walks the watermark past
I, and if the device is then offline at release, I never appears on it.

**The app tab's fix, adopted: cap R's subscribe fence just below the oldest invite held
from R in that channel.** Then the watermark can't pass I, and the first reconnect after
release pages forward through it. The cap is the newest row R can see below I, or `""`,
so the pager's `cursor >= fence` end condition stays reachable. Only the *subscribe*
fence is capped. The read-position bound keeps `latest_ulids`, so reading L still marks
L read.

**The cap hides what's above it, so the island replays that.** The client treats
everything above the fence as live and never pages it. So a device that missed L while
offline would not get L from the pager either. In first contact the call is the DM's
first row, the cap is `""`, and nothing is paged at all. Right after the `suback`, the
subscribe sends this connection the rows above the cap that R can see (L, never I) as
ordinary `message` frames, **all of them, drained page by page with no count limit**
(Tesla, round 1: a fixed count let a stranger push the tail past it; the hold window
already bounds it), each carrying the reaction aggregate a history read would (the
same `message_views` builder history uses). The app inserts a live frame by id
whatever the fence says, and dedups on the id (app tab, answer 3). **Messages only.**
v2 justified that by `envelopes.retraction_frame`'s claim that a retraction's id
advances the watermark. The app tab read its code: a live retraction never moves the
watermark, which only ever becomes the subscribe fence. So the justification was
wrong, and that docstring is corrected. Retractions stay out because they heal on the
next history walk and a live one only removes.

**One statement decides both the fence and the cap** (v3), per channel. So the cap
cannot be computed against a different snapshot from the fence it caps (the same
class as above).

## Residuals, named

- **A recipient whose clock runs more than 50 s behind the island's** can still be rung
  by the release frame. The app already accepts the mirror tradeoff for senders ("a peer
  whose clock is >10 s off never rings us").
- **Invites persisted before deploy** aren't covered by the ingest cap. A pre-deploy row
  more than two minutes old is outside the scan and visible, exactly as it is today.
  Deploy check: count DM call/2 invites with `signed_at_ms > created_at + 60 s` on both
  boxes; expect 0.
- **The stranger's END needs no hold.** It arrives live for a call the app never saw,
  and the app ignores it. A reply preview pointing at the held invite gets a 404 for up
  to a minute, which is cosmetic.

## Tests (the must-fail triple, ported from app branch `feat/ring-no-prior-conduct`)

Each arm fails if exactly one conjunct is removed:
- **Stranger + fresh → hidden.** Every door: not in history, not in the fence, not in the
  DM preview, 404 by id, excluded from the live fanout. Fails if the hold is deleted.
- **Control: `R` posted first → visible immediately, on every door.** Fails against a gate
  that holds every invite, which would be an outage of calling, not a stranger gate.
- **Stranger + past `release_at` → visible, and the sweeper sends it to `R` only.** Fails
  against a gate that withholds forever, which would contradict "keep the missed call".

Plus:
- `R` posts *during* the hold → still held (order, not time).
- An agent sender → not held.
- An END → not held.
- A block made during the hold → not released.
- A future-dated invite → `bad_origin` for a friend and a stranger alike.
- The wake gate and the hold agree when `R` posts between write and wake.
- Restart: a hold spanning a sweeper restart is still released.

## The app tab's answers (2026-10-29, read from aiko_chat_app main)

1. **No backwards read anywhere**, and v1's residual mechanism was wrong. See "The
   subscribe fence is capped" above, which is the app tab's proposal.
2. **The cap is acceptable.** A per-message error goes `_onError` →
   `_cache.markFailed(refClientMsgId)`, so the caller sees the "started a call" line
   marked failed, with the usual retry. Every receiver already refuses an invite signed
   more than 5 s in its future (0.0.7 allows 0 s), so the cap turns away only calls
   that could never ring.
3. **Confirmed.** `MessageFrame` → `_onMessage` → `_persistInbound` → `verifyOrigin` →
   `_cache.upsertInbound`, which dedups by island id and never compares against the
   fence. On insert, `admitRing` refuses an invite signed more than 10 s earlier as
   `stale`. Rows are ordered by island id, so a late older id slots into place.

Also agreed: agents aren't held, and the END isn't held.

## Out of scope

Friends (#2792): the hold is conduct, not consent, exactly as `_spoken_here` is. A ring
ceiling the island owns stays reversed (Nick, 2026-09-15). Group calls: not a DM, so
never rung.
