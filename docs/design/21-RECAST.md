# Design 21 recast: after three review rounds

**v2, 2026-10-29: folds the design temper (`21-TEMPER.md`, RECAST 4/4).** The deletions
held. The clock section of v1 did not: it fused three questions into one clock and
produced a worse failure than the one it fixed. The v1 formula is retired below.

**Why this exists.** PR #445 (design 21, the stranger's call hold) reached the three-round
cage-match cap with real findings still arriving, a growing diff, and two of Round 9.8's
stop signals firing: my own fixes generated findings (round 1's high-water clock produced
round 2's detune; round 2's catch-up clamp produced round 3's dropped releases), and two
finding classes repeated across rounds. Nick's 2026-08-23 ruling: no fourth round; a design
pass that opens with the producer question. Nick 2026-10-29: **delete the sweeper**, and
run the pass now.

What the PR got right holds and is not re-litigated here: the hold itself (Nick's
decision), one SQL clause decided inside the statement that reads each row, the capped
subscribe fence with its replay, one conduct definition shared with the wake gate, the
ingest cap, and the reaction door. This note is about the two classes that kept producing
findings.

## Class 1: delivery of the released invite (the sweeper)

**Findings it produced (4):** a release reached by no socket read as delivered (Noether r1);
a failing tick grew its scan without bound (Noether r1); the clamp that bounded it dropped
releases and advanced outside its `try` (Tesla r3, produced by the r1 fix); one socket that
stopped reading froze every later release on the island (Tesla r3).

**The bad outcome it guards:** a released missed call sits unseen on a device that stays
connected, because the app pages history only on (re)subscribe.

**The producer, and can it be removed?** The producer is the island pushing a late frame
from a background task. It can be removed, and Nick removed it: **no sweeper**. What made
that safe is already in the PR. The subscribe fence is capped below the oldest held invite,
so no device's watermark can pass it, and the first walk after release pages through it.
Nothing is lost; only latency changes. **The latency bound is the next socket loss**, not
the next foreground (the app tab, from its code: a live socket ignores resume kicks, and
the island closes one only on revocation):
- iOS: roughly the next real foreground (suspension kills the socket).
- Android: a quick background may not drop it.
- An always-on desktop: **days**, bounded in practice by island redeploys.

v1 said "possibly hours"; Nick decided on that, so the corrected number goes to him.

**What goes:** `realtime/call_release.py`, `call_hold.due_releases`, the lifespan task,
`hub.fanout`'s `only_user_ids` and delivered-count return, and their tests. With them go
four findings and a background loop that queried the database every 2 s for the life of
the process.

**What the app tab needs to hear:** it voted for a delayed live send (2026-10-29), on the
reasoning that history-only delivery surfaces the missed call hours late on a desktop.
That consequence is now accepted on purpose, for a stranger's call, in exchange for
removing a producer of four defects.

## Class 2: time

**Findings it produced (4):** a backward wall-clock step re-held a released invite,
shrinking visibility (Tesla r1); the hold's high-water clock and the raw-wall-clock ids
detuned, and after a step back of more than two minutes a fresh invite read as released
(Tesla r2, produced by the r1 fix); `new_ulid`'s random tail let a reply in the invite's
millisecond sort before it and count as conduct (Carnot r2); a **forward** wall-clock step
counted as elapsed hold, releasing a still-ringable invite (Carnot r3).

**The bad outcome:** a stranger's invite released while the recipient's app would still
ring it, or conduct read off an id order that isn't causal order.

**The producer:** the island's wall clock used as a stopwatch. It answers two different
questions, and they need two different clocks:
- *How long since the island received this?* is **elapsed** time, which a stopwatch should
  answer. A wall clock is the wrong instrument: a step in either direction is read as
  elapsed time.
- *Is the caller's signature too old for the recipient?* is **wall** time against wall
  time, because the app judges `signedAt` against its own wall clock. The island can only
  approximate the recipient's clock with its own.

**Can it be removed?** The dependence on time can't be removed: Nick's decision ("keep the
missed call") needs a release instant. The wrong instrument can be.

**v1 (RETIRED; do not build).** v1 proposed one clock, `max(previous, min(wall,
boot_wall + monotonic))`, for everything. The temper found it fuses three questions into one
instrument:
- The ingest cap read it, so an axis lagging real time by more than 60 s refused **every**
  DM invite, friends included, until a restart (Noether, Tesla). That's an outage of calling.
- The mint path and the seed still wrote raw wall time into it (Tesla).
- A forward step across a restart still released early (Carnot, Noether).

**v2: two instruments, each asked only its own question.**
- **Time** (is this invite still ringable? is this signature too far ahead?) is answered by
  the **wall high-water clock**, `call_hold.now_ms`: wall time that never runs backwards in
  this process. The hold's expiry is `max(signed_at, created_at) + HOLD > now`, where
  `created_at` is the wall clock at insert. **The hold never reads time off an id.** The
  ingest cap compares the signature with the same clock, so it can never lag an honest
  signature unless the island's own wall clock does.
- **Ordering** (did the recipient post before this invite?) is answered by the **monotonic
  id axis**, `ids.next_axis_ulid`: strictly increasing, seeded above the store at boot. Its
  timestamp is a sort key, not a clock anyone reads. Carnot r2's same-millisecond
  misordering stays fixed, and Tesla r2's detune can't recur, because nothing compares the
  two.
- **Backward wall step:** the high-water freezes, so visibility never shrinks (Tesla r1). A
  hold lasts at most `HOLD + FUTURE_CAP + (the step)`, which fails toward later, never
  earlier. That restates design 21's "≤ 2 min" bound, which is only true without a step.

**The one residual, stated without a false claim:** the hold is exactly as good as the
island's wall clock against the recipient's, within 50 s. A step that makes the island's
clock wrong is an instance of it, in either direction, inside a process or across a
restart. A step that *corrects* the island's clock is not, and no island clock can tell the
two apart. Elapsed time can't rescue this: a signature legitimately ahead of receipt (inside
the cap) has to be judged wall against wall anyway. **Nick accepts or rejects this
residual; it is not the author's to wave through** (Carnot).

**Precondition (Maxwell).** Ordering is causal only while one process mints. `worker_guard`
plans to lift single-worker for #46; a shared axis (one minter, or ids taken from the
database) goes on that lift's list. Not built now: live delivery is per-process too.

## Class 3 (single finding, latent): the live door's candidate set

`held_viewers` read the DM pair off the `dm:` key and returned an empty set when it
couldn't, which read as "nobody is held", so the live door would deliver what every read
door holds (Noether r3; unreachable today because `create_outbound` refuses a malformed
key). Recast: the live door evaluates `held_clause` for **every raw `Membership` row of the
channel plus the canonical pair when it parses**. That's every user who could hold a
subscription, decided by the same clause the reads use, with no dependence on the key
parsing.

**v2 (Tesla):** membership ∪ pair on every send is a second check on a producer
`create_outbound` already removed. Keep the canonical pair, and fall back to raw
`Membership` rows **only when the key doesn't parse**: one branch, failing closed instead of
reading as "nobody".

## The replay drain (v2, Carnot)

The drain stops at the newest id the fence statement saw; everything after it arrives live
on the already-subscribed socket, so the drain can't chase a growing tail. Its work per
subscribe is the DM's traffic between the oldest held invite and that cursor: two minutes,
plus any backward step. The island has **no WebSocket send rate limit at all**. That's
pre-existing (a stranger can flood a DM live today with no hold involved), it bounds live
delivery and the replay alike, and it's filed as its own task rather than given a
hold-specific throttle.

## What the recast PR will contain

1. Delete the sweeper (class 1).
2. Two instruments (class 2, v2): `call_hold.now_ms` is a wall high-water owned by
   `call_hold`, read by the hold and the cap; the hold's receipt term reads `created_at`;
   `ids.next_axis_ulid` keeps its own monotonic state for ordering only, and
   `ids.axis_now_ms` is deleted.
3. `held_viewers`: canonical pair, with a membership fallback only on an unparseable key.
4. The drain gets a terminal cursor.
5. Round 3's boot-seed fix stays (`17fe0c6`).
6. Design 21 updated in place, and its residuals list re-derived. A precondition line for
   multi-worker, plus the lift condition in `worker_guard`.
7. Tasks filed: a WebSocket send rate limit; a comment on #46.

Then a fresh cage-match round 1 on the result.
