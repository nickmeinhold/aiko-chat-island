# 21-TEMPER.md — Design 21 recast (after cage-match #445's three rounds)

**Overall verdict:** RECAST, 4/4 families (no DISSOLVE). The deletions hold; the clock
fusion does not.
**Struck:** dt-1793252673. Families seated: Maxwell, Carnot, Noether, Tesla (Wu disabled).
**Bundle:** `21-RECAST.md` v1, `21-a-strangers-call-waits-until-it-cannot-ring.md` v4,
`call_hold.py`, `ids.py`, all at `17fe0c6`.

## Per-family verdicts

| Family | Verdict | One line |
|---|---|---|
| Maxwell (Claude) | RECAST | The axis is process-local while `worker_guard` plans multi-worker (#46); the "hold ≤ 2 min" bound is no longer true |
| Carnot (GPT) | RECAST | Ordering, elapsed age and signature age are three questions; a forward step across a restart still releases early; the drain has no terminal cursor |
| Noether (DeepSeek) | RECAST | The ingest cap reads the axis, so a lagging axis refuses every honest call; a forward step across a restart; a never-reconnecting device |
| Tesla (Grok) | RECAST | One ratchet asked two questions: a lag over 60 s blacks out every DM call, friends included, until a restart; the mint path and the seed still write raw wall time into the axis |

## Fatal flaws (deduped, most severe first)

- **The recast fuses three questions into one clock** (all four families, in different
  words). Ordering (which id is earlier), elapsed time (how long since receipt) and
  signature age (wall against wall) each need a different instrument, and the recast's
  `max(previous, min(wall, anchored))` serves all three.
  - **The cap reads it, so an axis that lags real time by more than 60 s refuses every DM
    invite, friends included, until a restart** (Noether, Tesla). This is the most severe:
    an outage of calling, produced by the fix for a narrower fault.
  - **The mint path and the seed still write raw wall time into it** (Tesla), so the first
    id after a forward step defeats the `min`.
  - **A forward step across a restart still releases early**, because the boot anchor reads
    wall time again (Carnot, Noether). The recast claimed forward steps no longer
    contribute, and that claim was false.
  - DISPOSITION: **dissolve the fusion; fold into 21-RECAST v2.** Two instruments, each
    asked only its own question:
    - The **wall high-water clock** answers every time question: the hold's expiry and the
      ingest cap.
    - The **monotonic id axis** answers only ordering (conduct); the hold never reads time
      off an id.
    - The hold's receipt term reads `created_at` (wall at insert), not the ULID prefix.
    - The cap can then never lag honest signatures, and ids can't detune the hold, because
      the hold doesn't read them.
    - What's left is one residual, stated without a false claim: **the hold is exactly as
      good as the island's wall clock against the recipient's** (±50 s). A corrupting step
      in either direction, across a restart or not, is an instance of it. **Needs Nick's
      explicit acceptance** (Carnot asked for exactly that).
- **The stated hold bound is false under a clock fault** (Maxwell, Tesla). DISPOSITION: restate
  it as `HOLD + FUTURE_CAP + (backward wall step)`, which fails toward later, never earlier.
- **The drain has no terminal cursor, and its workload has no stated bound** (Carnot).
  DISPOSITION: drain to the newest id the fence statement saw; rows above it arrive live on
  the subscribed socket. The workload is two minutes of the DM's traffic per subscribe, and
  the island has **no send rate limit at all** (pre-existing; filed separately, not given a
  hold-specific throttle).
- **The ordering axis is process-local while the guard plans to lift single-worker for
  #46** (Maxwell). DISPOSITION: record single-process as a precondition in design 21, add a
  shared axis to `worker_guard`'s lift conditions, and comment on #46. Don't build it now:
  live delivery is per-process too.
- **A device that never reconnects** (Noether; the app tab corrected the bound from its
  code). DISPOSITION: named tradeoff. The bound is the **next socket loss**, not the next
  foreground: on iOS roughly the next real foreground; on Android a quick background may not
  drop it; on an always-on desktop, **days**, bounded in practice by island redeploys. Nick
  decided on "possibly hours", so he gets the corrected number.
- **Class 3's candidate set** (Tesla against Noether r3). Tesla: membership ∪ pair is a second
  check on a producer `create_outbound` already removed. DISPOSITION: keep the canonical pair;
  only when the key doesn't parse, fall back to raw `Membership` rows. That's one branch,
  failing closed instead of reading as "nobody".

## What holds

- Deleting the sweeper, made lossless by the fence cap (all four; the app tab confirmed against
  its code, pinned by its test "live retraction is SUPPRESS-ONLY").
- The correlated hold clause and the one-statement fence/cap (all four).
- A strictly increasing message axis for ordering (Carnot, Maxwell), now asked only that.

## Disposition

RECAST. Fold into `21-RECAST.md` v2, take Nick's acceptance of the wall-clock residual and
the corrected latency bound, build, then a fresh cage-match round 1 (the round cap reset by
the design pass, per the 2026-08-23 ruling).

<!-- Each section below was written when that family returned, before the others
     finished. Arrival order, NOT a ranking. Round 6 replaces this header with the
     synthesized verdict; if it never runs, what is here is still real. -->

## MaxwellMergeSlam's Design Strike

**Verdict:** RECAST

**Summary:** The recast removes both repeating producers correctly (the sweeper by deletion, wall-time-as-stopwatch by a two-clock axis), but it hard-codes a process-local axis into a gateway whose own guard is scheduled to go multi-worker, and it states a two-minute hold bound that its own clock no longer guarantees.

**Producer first:** Class 1's producer (the island pushing late frames from a background task) is deleted, and the fence cap is what makes the deletion lossless. That's the right answer, and it removes four checks with it. Class 2's producer (the wall clock read as elapsed time) is replaced, not guarded. Class 3's producer (the live door re-deriving candidates by parsing the key) is replaced by the reads' own clause. One producer the recast creates and doesn't name: **the axis lives in process memory**, so ordering is causal only while exactly one process mints. That's ours, and it can be made structurally unable to break (below).

**Fatal flaws:**
- **Make cross-worker misordering impossible, before the guard that prevents it is lifted (unstated assumption, under-counted blast radius).** `worker_guard.py:40` says single-worker is to be lifted with `GATEWAY_ALLOW_MULTIWORKER=true` "once #46 is live". The recast's whole ordering premise (`prior.id < invite.id` is causal order, and receipt can't lead "now") holds only within one process: two workers mint from two `_axis_last`s and read two `axis_now_ms`s. The day #46 ships, a reply minted on worker B can sort below an invite minted on worker A in the same millisecond, which is exactly Carnot r2's release, and nothing reddens, because every test runs one process. Fold: name single-process as a precondition of the hold in RECAST and design 21; add "a shared axis (one minter, or ids taken from the database)" to `worker_guard`'s lift conditions next to #46; comment on #46 itself. Don't build the shared axis now: the hub is per-process too, so multi-worker is already unsupported for live delivery, and the hold isn't special. Recording it is what keeps the plan from walking into it.
- **The "hold ≤ 2 min" bound is stated as unconditional; under the recast clock it isn't (stated invariant the mechanism doesn't keep).** Design 21 says the cap bounds every hold to `receipt + 2 min`, and the replay-cost argument for the cap rests on that. But with `axis = max(previous, min(wall, anchored))`: (a) a seed above real time (the previous process ran fast) freezes the axis until real time catches up, so every hold, capped fence and replay lasts that long; (b) `time.monotonic` (CLOCK_MONOTONIC) doesn't advance during host suspend, VM live-migration pauses or `docker pause`, so the axis falls behind real time for the life of the process and every hold lengthens by the pause. Both fail SAFE (later, never earlier), so neither is fatal to the ring guarantee. But the stated bound is false, and the replay's cost argument inherits that. Fold: restate the bound as `HOLD + FUTURE_CAP + (axis lag behind real time)`, list both causes as named residuals, and log a WARNING when the axis and wall time disagree by more than HOLD, so an operator can see the residual happen instead of inferring it.

**What holds:**
- Deleting the sweeper is lossless *because* the fence cap already exists: the watermark can't pass a held invite, so the first walk after release reads it. That's a deletion backed by an invariant, not a dropped feature.
- `min(wall, anchored)` inside a running `max` is the right shape: forward steps can't advance it, backward steps freeze it (no visibility shrink), and a fast-at-boot clock self-corrects once wall time is right. Every fault lands in the "later" direction.
- Class 3's candidate set (members ∪ pair, decided by `held_clause`) makes the live door and the reads one definition again, with no key parsing on the path.
- The recast opens with producers and deletes machinery. It's the opposite of rounds 1–3's drift toward more guards.

**If RECAST, what to fold back:**
- RECAST + design 21: a "Preconditions" line: the hold's ordering is causal only with one minting process; multi-worker needs a shared axis first (worker_guard lift condition, #46 comment).
- Design 21: restate the hold-duration bound with the axis-lag term, and name the two lag causes (a seed above real time; monotonic pauses) as residuals in the safe direction.
- `ids.axis_now_ms`: log a WARNING once when `|axis − wall| > HOLD_MS`. That's not a guard, just the residual made visible.

## CarnotCodeCarver's Design Strike

**Verdict:** RECAST

**Summary:** Deleting the sweeper removes real machinery and its failure modes. The remaining design still confuses ordering with elapsed time, and a bounded hold does not bound replay work. No real engine matches the Carnot cycle; a reviewer's job is to say how far short we are.

**Producer first:** The harm is an unsolicited call ringing an installed app. Clock jumps and hostile invites are triggers; our producer is serving the signed invitation while the app still considers it fresh. Serving the original invitation cannot be deleted without abandoning the accepted missed-call requirement: installed apps ignore new fields and the signed body cannot be rewritten. However, using wall-derived ULID progress as elapsed age is our removable coupling. A second harm is a reconnect consuming unbounded database and socket work; our producer is the unlimited replay above a capped fence. Removing the sweeper correctly deletes another producer rather than hardening its retries.

**Fatal flaws:**
- make clock corrections unable to impersonate elapsed hold time: separate causal ordering from elapsed-age accounting; then the hybrid wall/ULID stopwatch becomes unnecessary. The proposed protection lasts only within one process. Example: receive an invite at T, restart five seconds later with the island wall clock stepped forward 120 seconds, and boot_wall now makes the invite immediately visible while the recipient reads T+5 and rings. Seeding above stored ids supplies ordering, not elapsed-time evidence. The named clock-skew residual technically includes this, but the claim that forward steps no longer contribute does not. Specify conservative elapsed-age semantics across restart, or explicitly obtain acceptance that restarting restores the exact early-release failure this recast claims to remove.
- make replay unable to chase an indefinitely growing tail: drain through a fixed upper boundary captured for that subscription. 'All of them, drained page by page with no count limit' gives no terminating boundary while new rows arrive. Even with a fixed boundary, two minutes bounds duration, not row count, bytes, reaction aggregation, or repeated reconnect cost. A stranger can keep fresh holds arriving and force repeated transmission of substantial overlapping history. The design needs an explicit workload bound or an existing admission limit demonstrated to provide one; deleting the sweeper does not delete this producer.

**What holds:**
- The accepted reconnect latency makes sweeper deletion a sound simplification; retain the fence cap so the missed call remains recoverable.
- The correlated hold clause and atomic fence/cap decision remove demonstrated snapshot races.
- A strictly increasing message axis addresses same-millisecond ordering, assuming the stated single-worker ownership and complete boot seed.
- Membership union canonical pair removes the live door's dependence on successful key parsing.
- The recipient-clock limitation is explicitly acknowledged; the island cannot establish recipient freshness exactly.

**If RECAST, what to fold back:**
- Keep sweeper deletion and the membership candidate change.
- Define ordering, elapsed age, and signature wall time independently. State restart behavior and test a forward correction across restart, including a corrected wall clock behind the persisted seed.
- Give subscription replay a fixed terminal cursor and substantiate its maximum work under sustained invites and reconnects.
- Replace the superseded v4 clock, sweeper, residual, and test sections so the bundle expresses one contract.
- Dijkstra: "Simplicity is prerequisite for reliability." Remove the clock coupling before adding another compensating mechanism; its accumulated entropy is already visible.

## NoetherNullPointer's Design Strike

**Verdict:** RECAST

**Summary:** The invariant at stake is “a forward wall-clock step can never advance the hold clock, and the hold clock never lags real wall time enough to refuse honest calls”; the recast breaks both at boot and in the ingest cap.

**Producer first:** The change deletes the sweeper — a producer of four defects — and hardens the hold clock and the live-door candidate set. The hold clause guards the harm “a stranger’s call rings an open app”; its producer is the island delivering the invite over the socket to an app that rings it. For installed apps that producer cannot be removed, so the hold clause must stay. The ingest cap guards “a future-dated invite holds for a day and caps the fence”; its producer is a stranger signing a far-future `signed_at_ms`. The signed body is untrusted, so the cap is needed, but its current use of the axis clock is a producer of false refusals. The class 3 recast removes the producer “malformed `dm:` key makes `held_viewers` return empty”; that is a correct producer removal. No check added for the sweeper deletion; that is a producer removal with an accepted latency tradeoff.

**Fatal flaws:**
- **Forward wall-clock step across a restart releases still-ringable invites.** In the recast clock `max(previous, min(wall_now, boot_wall + monotonic_elapsed))`, at boot `monotonic_elapsed` is 0, so `min(wall_now, boot_wall)` is `wall_now`, and `max(previous, wall_now)` jumps the axis to the post-step wall clock. An invite received before a forward step during downtime can be released immediately after restart while the recipient’s app still rings it. This violates the invariant that a forward wall step cannot advance the hold clock (Carnot r3). Class: broken invariant / wrong clock initialization.
- **The future-dated ingest cap uses the axis clock, so a lagging axis refuses honest invites.** `refuse_future_dated` compares `signed_at_ms` to `now_ms()` (axis). The recast clock can lag real wall time by more than `FUTURE_CAP_MS` — a slow boot, or a freeze after a backward step. Then an honest caller’s `signed_at_ms` is more than 60 s ahead of the axis, and the invite is refused as `bad_origin`. The design calls slow-boot lag “safe” and “cosmetic”; it is neither: it causes an outage of calling. Class: wrong instrument / coupled clock.
- **Deleting the sweeper reintroduces the original bad outcome for never-reconnecting devices.** A released invite is delivered only on reconnect. An always-connected desktop that never reconnects never sees the missed call, contradicting “keep the missed call.” The design accepts latency but does not name the never case. Class: missing failure mode / under-counted blast radius.
- **The provided `held_viewers` still uses `canonical_peer_ids` only**, contradicting the recast’s class 3 fix. If the recast PR is built, it must actually implement the membership ∪ pair candidate set. Class: design/code inconsistency.

**What holds:**
- The hold rule (conditions 1–5) and the one-SQL-clause decision inside the statement that reads the row.
- The removal of the sweeper removes a producer of four defects; the latency tradeoff is explicit.
- The class 3 recast (membership ∪ pair) correctly removes the malformed-key producer.
- The monotonic id axis with an incrementing tail for causal conduct.
- The subscribe fence cap and replay.
- The no-primary-key floor in the hold.

**If RECAST, what to fold back:**
- Decouple the future-dated check from the axis clock: use a wall clock (or a separate `wall_now_ms()`) for `refuse_future_dated`, so a lagging axis cannot refuse honest invites.
- Fix the axis clock at boot to not jump forward on a wall-clock step during downtime. Either persist a monotonic anchor, or explicitly name “a forward wall-clock step during downtime can release a still-ringable invite” as a residual and accept it. Do not claim forward steps cannot advance the clock across restarts.
- Implement the class 3 `held_viewers` candidate set as membership ∪ canonical pair, not the current `canonical_peer_ids` only.
- Clarify the sweeper removal: if an always-connected device never reconnects, the missed call is never seen. Either accept and name this residual, or provide a live release that does not have the sweeper’s defects.
## Tesla, the Arc-Prophet's Design Strike

**Verdict:** RECAST

**Summary:** The sweeper's silence is true, and the new axis is one ratchet asked two questions — the first wall step louder than sixty seconds blacks out every DM call until the process is born again.

**Producer first:** The recast hardens two checks by feeding them a new clock, and it deletes a producer rather than guarding it. The sweeper's harm (a connected device never sees the missed call, because history pages only on subscribe) was produced by a background task pushing a late frame. That task is removed. Nothing replaces it. That shape is right.

The clock's harm, as harm: a person places a call, friend or stranger, the island answers `bad_origin`, the handset draws the call failed, and the other party neither rings nor ever receives the missed-call line. The trigger is external — a forward wall step, or time passing while the monotonic clock does not (suspend, VM pause). The producer is ours. Class 2 sets `axis_now_ms` to `max(previous, min(wall_now, boot_wall + monotonic_elapsed))`, and `refuse_future_dated` reads that value through `now_ms()`. After a forward discontinuity of size L, `min` sits on the monotonic branch and `max` forbids stepping up to the corrected wall, so the axis stays L behind an honest `signed_at` for the life of the process. `FUTURE_CAP_MS` is 60s. When L is louder than that, the cap rejects every DM invite. Remove the fusion: the cap compares a signature to a high-water of raw wall, and the hold's elapsed gate reads a monotonic sample stored at insert. The blackout then cannot be produced, and no third guard belongs in its place.

Class 3's harm (an open app rings because `held_viewers` returned nobody) is produced by a write into a channel the door cannot name. `create_outbound` already refuses a malformed `dm:` key, which is the only path the recast calls reachable. The membership∪pair scan is a second check on a producer that is already gone.

**Fatal flaws:**
- Class 2 of `docs/design/21-RECAST.md` (the formula and the residual it names "holds run longer, safe" and "axis ids lag `created_at`, cosmetic"), wired through design 21's clock section, `call_hold.now_ms`, `refuse_future_dated`, and the `signed > window` arm of `held_clause`. Sixty seconds is both the hold and the cap, so it is the resonant frequency. A step, a resume, a pause wider than that note does not lengthen a hold. It stops ingest. Invites already inside release on the old schedule. Invites after the step never enter. Friends fall with strangers, because the cap was built to be indiscriminate so it would reveal nothing — the blast the residual under-counts. The process does not heal. Only a restart samples `boot_wall + 0` and meets the wall again. The residual's name will keep the 3am reader from restarting anything. The inputs this recast exists to survive (the steps larger than two minutes from rounds 1–3) are the note that shatters it. Tesla: "If you want the secrets of the universe, think in energy, frequency and vibration."
- The harmonic of that same coupling. `held_clause` stays held while the ULID looks young or `signed > window`, and the ULID's time prefix is the axis. `ids.py` has three writers of `_axis_clock_ms` — `axis_now_ms`, `next_axis_ulid`, and `seed_axis` — and the mint path still does `max` with raw `time.time()`. The recast retunes the reader and leaves the writers. The first id minted after the step high-waters the global, the clamp becomes a lullaby, and the early release from round 3 walks back in. Move every writer onto the ratchet and the receipt term is still that prefix: `signed > window` stays true for the size of the step, the subscribe fence stays capped that long, and every subscribe replays the tail above the invite. That is the day-pin design 21's future cap exists to forbid, returned at whatever size the clock stepped. Pointing only the cap at raw wall, and leaving this OR-term on the ratchet, trades the blackout for the pin.

**What holds:**
- Deleting the sweeper. The four findings were the background frame. The capped subscribe fence, and the uncapped walk on the next subscribe, keep the missed call. A socket that never resubscribes waits. That latency is written down as accepted.
- One clause, decided by the statement that returns the row. Conduct as id order on one strictly increasing axis, seeded above the store at boot. Visibility moves one way while the clock judging it cannot step backward under the row.
- Persons, DMs, invites, no prior post. The wake gate on that same bound. Agents and ENDs outside the hold. Retractions on the capped replay. A future signature refused for every caller, while the clock the cap reads is actually wall.

**If RECAST, what to fold back:**
- In design 21's clock section and Class 2 of the recast, replace the single `axis_now` with three instruments, and one sentence that forbids them sharing a variable. Cap clock: `refuse_future_dated` compares `signed_at` to a high-water of raw wall. It follows a forward correction. A backward step must not make an honest signature look future. It is never `min`'d with monotonic time.
- Elapsed gate: at insert, store the boot generation and a monotonic sample beside the wall receipt already in `created_at`. `held_clause` requires the same boot and `mono_now < mono_at_insert + HOLD`. A wall step in either direction is not elapsed, and it cannot freeze the gate. Across a restart the epoch is dead: keep holding until this process has been up for `HOLD`, then judge the wall deadline. That wait is the safe direction. A jump during downtime cannot hand history a still-ringable invite. The ULID stops being the stopwatch. `ulid_floor(now − HOLD)` leaves the time term.
- Signature gate, one-way: `high_water_wall >= signed_at + HOLD`. High-water never falls, so a backward step cannot re-hold and the pager stays unhung. A forward step may clear this gate early. The elapsed gate still withholds until real `HOLD` has passed, so the step cannot ring. With the cap at 60s this gate clears by 120s, and the fence pin stays inside the bound design 21 already accepted. Seed the high-water at boot from `max(raw wall, newest stored created_at)`, so a step back across restart cannot re-hold an invite the previous process had already released.
- Ids: `next_axis_ulid` remains causal order for conduct. Delete its `time.time()` write into the hold clock. An id may lag `created_at` only after the hold no longer reads the prefix. Those words ("cosmetic") become true after the split, and only then.
- Residuals: strike "slow at boot, holds run longer, ids lag, cosmetic." The residual that remains is the one v1 named — a recipient more than 50s behind the island can still be rung. A pre-deploy invite with no monotonic sample stays visible, as the deploy check already expects of rows outside the window.
- Class 3: drop the membership union. The writer already refuses the unnameable channel. One less candidate set that can fail open by growing.
- The third arm of the must-fail triple: the invite is visible to history and to the next subscribe, and no background task exists to send it.
