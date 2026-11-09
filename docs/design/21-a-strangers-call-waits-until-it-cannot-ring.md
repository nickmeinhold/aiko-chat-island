# Design 21: a stranger's call waits until it can't ring

**Status: v5, BUILT, fresh cage-match round 1 pending.** #445 hit the three-round cap; the
design pass is `21-RECAST.md` (opened with the producers of the two repeating classes) and
`21-TEMPER.md` (RECAST 4/4). v5 is the recast as built, with one correction found while
building it (the time clock is the raw wall clock, not a high-water mark; below). v1 was
written 2026-10-29 before any code. v2 folded in the app tab's answers to the three questions v1 asked (same
day, read from its main); answer 1 corrected v1's model of the residual and supplied
the fence cap. v3 folds round 1 of the PR's cage-match (#445: 3/3 REQUEST_CHANGES, ten
findings, eight real), most importantly that the hold is decided **inside the statement
that reads the row** (below). v4 folds round 2 (3/3 REQUEST_CHANGES, seven real): **one
clock and one monotonic id axis** for the whole hold, the fail-open primary-key floor
removed, retractions replayed, and the reaction frame added to the doors.
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
  - It bounds how long any hold lasts: `max(signed, receipt) + HOLD <= receipt + 2 min`.
    Without it, a stranger could sign an invite a day ahead and hold it for a day, and
    for that day the recipient's subscribe fence would stay capped below it, with every
    subscribe replaying everything after it. (v2 also used it to make a primary-key
    floor complete; v4 removed that floor, below.)
  - The refusal applies to **every** caller, friend or stranger, so it reveals nothing
    about the recipient's conduct. An honest caller whose clock is a minute fast already
    fails every ring (the app refuses it as `clockSkew`); now they get an error instead.
  - A non-integer `signed_at_ms` is refused too (v4). `signing.validate_origin` already
    rejects one at the WS door, but the SQL clause casts the stored value, so the two
    readers must agree whatever writer comes next (Noether, round 2).
- **No primary-key floor in the hold (v4).** v2 and v3 carried `id >= ulid_floor(now −
  HOLD − F)` inside the hold clause as an index bound. It was a second answer to the
  question the time term already answers, and it failed OPEN: whenever the two
  disagreed, a still-ringable invite was released (Tesla, round 2). The clause now leads
  with its cheapest terms (the GLOB first), so an ordinary message costs one string
  match. **No id floor anywhere (v5).** The same floor came back as a "candidate bound"
  on the cap and forward-stop lookup (`_oldest_held`), which read time off an id and
  failed open the same way: a held invite whose id sorted below it escaped the cap and
  the stop, and a walk could pass the missed call (3/3 seats, recast round 3). It is
  gone. The lookup's cost is met by scope instead: only a DM can hold a call, so other
  channels answer without scanning.

## The doors

Built by grepping every caller of `message_view`, `message_frame`, `get_history`,
`latest_ulid(s)`, `last_visible_message(s)` and `visible_message`, not from memory.
v3 adds a second grep, every `hub.fanout(` site, because a frame that NAMES a message
is a door even when it carries no row; v2's grep missed the reaction frame.

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
| Live reaction frame | `rest/reactions._fanout` | no | held viewers excluded (v3; missed by v2's table, found in round 2: a caller reacting to their own held invite told the recipient a hidden row existed) |
| Live retraction frame | `rest/moderation` → `hub.fanout` | no | **unfiltered, by design**: a retraction carries no content and only removes (the add/remove asymmetry) |
| Wake (push) | `push_service.wake_for_message` | rings a **locked** phone | already gated; moves to condition 4's bound |
| Subscribe replay | **new**: only when the fence is capped | yes (live frames) | replays the visible rows above the cap; never the held one |

The app tab read its own main on 2026-10-29 14:22: **only two of these can ring the app**,
WS message frames and REST history. The others stay on the list for visibility to other
clients, and for federation later.

**One predicate.** `get_history`, `latest_ulids` and both `last_visible_message`
variants already share `moderation_service.not_blocked_predicate`, and the fence's
docstring explains why they must: the reconnect pager runs until `cursor >= fence`.
The hold becomes part of that shared predicate (`messages_service._visible`), and
`visible_message` reads through the same predicate in one statement. **The hold adds
one case to the documented fence/pager shrink race** (v5; this paragraph said the
opposite until Tesla, recast round 2). The hold's clock is the raw wall clock, so a
backward step can make a released invite held again. If a subscribe fence already
names it (first contact: the invite was the DM's only row), the walk gets an empty
page before the fence: design 04's guard, which heals on the next reconnect, exactly
as for a block or a takedown. It can't ring, and it can't strand the missed call,
because forward pages never cross a held invite. A one-way latch would remove it, but
a latch is a high-water mark by another name, and that releases fresh invites early
after a correcting step (see "Two instruments").

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
- time reads `created_at` and the signature's JSON field, both wall-clock readings, and
  never the id (v5; see "Two instruments").

The send exclusion (`held_viewers`) asks the same clause, so there is one definition of
"held", not a SQL one and a Python one.

**Two instruments, each asked one question (v5).** Rounds 1–3 kept finding clock faults
(a re-hold on a backward step, a hold clock detuned from the id clock, a same-millisecond
misorder, a forward step counted as elapsed time), and the recast's first draft fused
everything into one clock that would have refused every DM call whenever it lagged real
time by more than 60 s (21-TEMPER). The questions are different and get different
instruments:
- **Time** (still ringable? signature too far ahead?) is the island's **raw wall clock**,
  `call_hold.now_ms`, against wall-clock readings: `signed_at_ms` and `created_at`. That
  is what the app does with the recipient's wall clock. **Not a high-water mark:** found
  while building v5. A high-water freezes ahead of real time after a step that *corrects*
  a fast clock, and every invite received during the freeze would be judged against a
  future "now" and released while still ringable. The raw clock costs Tesla's round-1
  finding: a backward step can re-hold a just-released invite for at most the step. That
  is a visibility shrink of the same self-healing class as a block or a takedown, and it
  can't ring.
- **Ordering** (did R post before this invite?) is the **monotonic message axis**,
  `ids.next_axis_ulid`: strictly increasing within the process (same or earlier
  millisecond means the last tail plus one, Carnot round 2), seeded at boot above the
  newest stored id that is a ULID (Maxwell round 3). Its timestamp is a sort key that may
  run ahead of the wall; **nothing reads time off it.** Ids not on the axis keep
  `new_ulid`.
- **The residual, accepted by Nick 2026-10-29:** the hold is exactly as good as the
  island's wall clock against the recipient's, within 50 s. A step that makes the
  island's clock wrong is an instance of it, in either direction, inside a process or
  across a restart. A step that corrects it is not. No island clock can tell the two
  apart.
- **Precondition:** ordering is causal only while one process mints. `worker_guard` plans
  to lift single-worker for #46; a shared axis goes on that list.

## Delivering it late (the missed call): no push (v5)

v2–v4 pushed the released invite from a background sweeper. It produced four of the
fifteen real findings (a delivery count that read "nobody listening" as delivered, an
unbounded retry window, a clamp that then dropped releases, and one stalled socket
freezing every release on the island). **Nick deleted it, 2026-10-29.** The capped
subscribe fence (next section) is what makes that lossless: no device's watermark can pass
a held invite, so its first history walk after the release reads it, as a missed call.

**The latency bound is the next socket loss** (the app tab, from its code: a live socket
ignores resume kicks, and the island closes one only on revocation). On iOS that's roughly
the next real foreground; on Android a quick background may not drop it; on an always-on
desktop it's **days**, bounded in practice by island redeploys. Nick accepted the corrected
bound. The late row lands at its own id, below anything the viewer has read, so it never
counts as unread. A future app release could re-walk on resume (app-only).

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
whatever the fence says, and dedups on the id (app tab, answer 3). **Retractions too
(v4).** v2 and v3 left them out, on `envelopes.retraction_frame`'s claim that a
retraction's id advances the watermark. The app tab read its code: a live retraction
never moves the watermark, which only ever becomes the subscribe fence, so that
docstring was wrong and is corrected. Leaving them out had a cost: a takedown above the
cap reached a reconnecting device on no door, and the deleted text stayed (Carnot and
Noether, round 2). The drain is `get_history` itself, so it serves exactly the stream a
walk would.

**One statement decides both the fence and the cap** (v3), per channel. So the cap
cannot be computed against a different snapshot from the fence it caps (the same
class as above).

## Residuals, named

- **The island's wall clock against the recipient's, beyond 50 s,** in either direction:
  a recipient whose clock lags the island's, or an island clock that a step made wrong
  (Nick accepted this, 2026-10-29; see "Two instruments"). The app already accepts the
  mirror tradeoff for senders ("a peer whose clock is >10 s off never rings us").
- **A hold lasts `HOLD + FUTURE_CAP` (two minutes) on a clock that doesn't step**, plus at
  most the size of a backward step. That step can also re-hold a just-released invite. It
  can't ring, and it can't strand the missed call: **forward history pages stop below an
  invite currently held from the viewer**, so no walk can set a watermark past one (Tesla
  and Carnot, recast rounds 1–2; first recorded as an accepted residual, then built,
  because the producer is a page crossing the hole, not the clock). The subscribe replay
  opts out (`cross_held`), because its frames never move the watermark. What remains is
  an **empty page before the fence** when a fence named the invite before the step: design
  04's guard, healed by the next reconnect.
- **Single process.** The ordering axis is per process; a shared axis comes before the
  single-worker guard is lifted (#46).
- **No WebSocket send rate limit** (pre-existing). It bounds live delivery and the
  subscribe replay alike: the replay re-sends at most the DM's traffic between the oldest
  held invite and the fence statement's cursor.
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
- **Stranger + past `release_at` → visible on every door.** Fails against a gate that
  withholds forever, which would contradict "keep the missed call".

Plus:
- `R` posts *during* the hold → still held (order, not time).
- An agent sender → not held.
- An END → not held.
- A future-dated invite → `bad_origin` for a friend and a stranger alike.
- The wake gate and the hold agree when `R` posts between write and wake.
- The hold never reads time off an id (an id an hour off either way changes nothing).
- The drain stops at the fence statement's cursor; an unparseable `dm:` key holds from
  the membership.

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
