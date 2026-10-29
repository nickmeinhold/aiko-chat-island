# Design 21: a stranger's call waits until it can't ring

**Status: v1, UN-TEMPERED.** Written 2026-10-29 before any code. It answers the
island half of the app tab's design 25 temper (aiko_chat_app#444,
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
| Subscribe fence | `ws._handle_subscribe` → `latest_ulid` | no rows, but history must agree with it | shared visibility predicate |
| Read-position bound | `read_positions_service` → `latest_ulids` | no | shared visibility predicate |
| DM list preview | `rest/dm.list_dms` → `last_visible_messages` | no | shared visibility predicate |
| Fetch by id | `rest/messages.get_message` → `visible_message` | no | 404 while held |
| Reactions | `rest/reactions` → `visible_message` | no | 404 while held |
| Wake (push) | `push_service.wake_for_message` | rings a **locked** phone | already gated; moves to condition 4's bound |
| Subscribe replay | none: `_handle_subscribe` sends only the `suback` | n/a | n/a |

The app tab read its own main on 2026-10-29 14:22: **only two of these can ring the app**,
WS message frames and REST history. The others stay on the list for visibility to other
clients, and for federation later.

**One predicate.** `get_history`, `latest_ulids` and both `last_visible_message`
variants already share `moderation_service.not_blocked_predicate`, and the fence's
docstring explains why they must: the reconnect pager runs until `cursor >= fence`.
The hold becomes part of that shared predicate (one `visible_predicate(viewer,
channel_ids, now)`), and `visible_message` applies the same rule. **Visibility only
grows over time under the hold** (held → released, never the reverse), so the
documented shrink race between fence and pager gets no new case.

The predicate is built in two steps, deliberately. First, one candidate query over the
PK range picks out human-sent DM rows that `R` didn't author, with no earlier post by
`R`. Then the **one parser** (`parse_call_body`) and `release_at` refine those
candidates in Python, and the read applies `Message.id NOT IN (held)`. A SQL `LIKE`
could only approximate the call/2 regex. The extra query is a two-minute PK range over
the viewer's DMs, and it's almost always empty.

## Delivering it late (the missed call)

History alone isn't enough. A connected client treats every id above its subscribe
fence as live, and its forward pager asks only for ids above its watermark. A held
invite becomes visible at an id **below** anything newer the client has since received.
With no live frame, any later message in the DM pushes the watermark past it, and the
missed call is stranded on that device. The app tab voted for a **delayed live send**:
a late frame with an older id slots into place by id order, and the app refuses to ring
it as `stale`.

**A release sweeper, not a timer per invite.** One background task runs every 2 s. It
re-runs the candidate query for invites whose `release_at` falls in `(last_sweep, now]`
and sends each one's `message_frame` to the held recipients' connections that are
subscribed to that channel. Before sending, it re-checks visibility, so a block made
during the hold wins.
- **Restart-safe by construction.** Its input is the database and the clock, so a hold
  that spans a restart is found again; there are no timers to lose. On boot it starts at
  `now`, because no socket is connected yet to receive what fell due while the island
  was down. Those clients reconnect and read history, where the invite is now visible.
- **A duplicate is harmless.** The app's cache is UNIQUE on the server id
  (`_handle_subscribe`'s docstring relies on the same fact), and a duplicate is stale
  anyway.
- **Hub change:** `fanout(..., only_user_ids=...)`, so the release reaches the recipient
  and not the caller, who already has it.

## Residuals, named

- **A device that was online during the hold and offline at release.** It received a
  *later* message, so its watermark is past the invite. It missed the release frame,
  then forward paging skips it. The missed call is absent **on that device** until
  something reads the newest page backwards. Whether opening a DM does that is the app
  tab's question (below). It needs a later message within 60 s **and** a disconnect at
  release.
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

## Questions for the app tab

1. Does opening a DM, or a cold start, fetch the **newest page backwards**
   (`before=None`)? If so, the first residual heals when the user opens the DM.
2. A call/2 invite more than 60 s ahead of the island's clock now gets `bad_origin`. Is
   that acceptable on the caller's side, and what does the user see?
3. Confirm that a live `message` frame whose id is below both the fence and the
   watermark is inserted by order and run through `admitRing` (expected: refused as `stale`).

## Out of scope

Friends (#2792): the hold is conduct, not consent, exactly as `_spoken_here` is. A ring
ceiling the island owns stays reversed (Nick, 2026-09-15). Group calls: not a DM, so
never rung.
