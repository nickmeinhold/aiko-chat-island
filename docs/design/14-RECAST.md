# 14-RECAST — the lease, named and priced; and §0's table split into three

**Answers:** [`14-TEMPER.md`](14-TEMPER.md), overall verdict **RECAST**, 4/4 families, struck
2026-09-13 against [`14-the-call-is-a-wire-object.md`](14-the-call-is-a-wire-object.md).
**Discharges:** temper flaw 1 (claude-tasks#4373) and temper flaw 6 (claude-tasks#4375) — the
two "before round 2" items this repo owns. Flaw 2's trilemma is cross-tab and remains
claude-tasks#4374.
**Written:** 2026-09-15.
**Bundle read before writing:** design 12 (Decisions 1b/1c and the D1–D5 table), 12a-TEMPER,
12a-RECAST (flaw 10), 14-TEMPER, the four raw strikes in `temper-strikes-14/`, claude-tasks
#3170 in full, #4325, #4265, #4327, #4326.

---

## The one thing to read if you read nothing else

**The temper did not refute this design. It renamed its object.**

Design 14 §2b offered `may_be_in = f(set, policy, occupancy)` as the answer to 12a temper flaw
10. Four families said the statelessness claim is false, and Tesla said the sharper thing: the
function answers the **wrong question**. The dissolved #3170 row was a **lease** — pairing
wakes, dropping lone ends, firing a stop. `may_be_in` is a **join ACL**, and co-presence only
has content at `n≥3`, which §5 of the same document declines to ship.

So the honest round-2 position is not a weaker version of §2b. It is a different object:

> **The island holds a LEASE per call — a record of its own ring, not a record of the call.**
> It has no endpoint, no schema, and no client read path. It is written only by the wake path,
> as a side effect of a decision the island was already making, and it expires on a deadline
> the island itself set.

That distinction is what clears #3170's DISSOLVE, and it is not a re-argument of #3170. It is
the observation that every objection the DISSOLVE raised was an objection to a row whose
**writer is the client**.

---

## Part 1 — the lease, named (temper flaw 1, claude-tasks#4373)

### 1.1 What it is

One row, keyed by the client-minted call ULID that wire v2 already carries:

| field | written when | read by | why it must exist |
|---|---|---|---|
| `call_ulid` | the island accepts an invite wake | the wake path | Kelvin's replay: without a spent-ULID memory a valid unexpired invite for a finished call re-wakes every participant |
| `issued_at` | same | the reaper | bounds the row's own life |
| `expires_at` | same, **set by the island** | the ceiling timer | Nick's 2026-09-09 ruling — the island owns the 30s ceiling; a ceiling needs something to fire against |
| ~~`woken` (the device rows charged)~~ | — | — | **REMOVED by §1.6.** The charge is paired by paying two at invite time, not by remembering who; the ceiling needs the tokens in process only |
| `state ∈ {ringing, settled}` | on end, or on expiry | the wake path | Carnot: *is this end lone* |

**That is the whole object.** Four of Carnot's four remembered facts, one deadline, no
participant set, no ACL, no occupancy.

### 1.2 Why it is not the row #3170 dissolved

#3170 closed 3/4 DISSOLVE. Design 12's table records the two findings that did the work. Both
are objections to a **client-written, client-read** object, and neither survives contact with
this one:

| DISSOLVE finding | what it objected to | why the lease is outside it |
|---|---|---|
| **D4 (Carnot)** — *"a write you may skip and a read you must not trust is not an API"* | `POST /v1/calls` + `GET /v1/calls/{id}` + a webhook + a schema. The client may forget to write, so a missing row cannot be distinguished from a call that never happened; the server may be lied to, so the read cannot be trusted. | **There is no write the client can skip**, because the client does not write. The island creates the lease as it decides to ring. **A missing lease is a definite answer** — no ring of ours is in flight — rather than an unknown. There is still no `POST`, no `GET`, no webhook and no schema. |
| **D1 (Tesla)** — *a server liveness detector is blinded by its own trigger* | a server-side notion of "is this call live", inferred from events the server only sometimes sees. | **The lease does not claim the call is live.** It claims *the island rang these tokens at T for this ULID, with a deadline of T+ceiling*. That is a record of the island's own act. It is never wrong about the world, because it makes no assertion about the world. |

The second row is the load-bearing one, and it has a precedent in this repo. **ADR-0008 turns on
the same asymmetry**: an operator signs *"I operate island X"* (unforgeable, because the signer
is the actor) and the island never signs *"I am operated by K"* (its own alibi). The lease is
that shape applied to a ring. An actor's record of its own action carries authority that an
observer's record of someone else's state cannot.

### 1.3 It clears the reopening bar — by a different route than design 12 thought

#3170's bar was *"name a server-side decision that must be authoritative."*

Design 12 Decision 1b recorded that bar as **already met**, by per-call membership under Nick's
2026-08-17 gathering ruling — i.e. by an **ACL** — and correctly scoped that as future work
gated on #3196. Design 14 then tried to clear the bar a second way, by arguing no state is
needed at all.

**Both routes are now superseded by a third, and it is cheaper than either.** The authoritative
server-side decision is not *who may join* — it is **whether the island is currently ringing
someone, and until when**. Only the island can answer that, because only the island did it. It
needs no gathering, no `n≥3`, and no #3196. It has content at `n=2`, which is the topology that
actually ships.

This is the direct answer to Tesla's cut. The lease is not *"a decorative object wearing a
function's clothes"* and it is not reopened on a gathering we will not ship.

### 1.4 What it costs, stated rather than discovered

1. **It is per-call state. The document that claimed statelessness was wrong, and §2b is
   retracted** as the answer to flaw 10. §2b's *content* about revocation (§2c) survives — the
   island supplying fresh policy at every gate is independent of the lease.
2. **A reaper is a new moving part**, and a lease that outlives its deadline is a ring the
   island thinks is in flight forever. Reaping must be a hard sweep, not a lazy read-time check,
   because the lone-end suppression reads it.
3. **Two-writer hazard.** The wake path creates and the end path settles. Concurrency discipline
   is owed and this repo has been bitten there before (`reference_sqlite_concurrency_dual_mechanism`
   — prod SQLite makes `FOR UPDATE` inert; an atomic conditional write is the shape).
4. **It is a trust-boundary and state-lifecycle change: cage-match by law**, per CLAUDE.md.

### 1.5 ⚠️ The one thing that is NOT mine to decide

**A lease holds, for its window, exactly the fact Nick ruled the island would not keep.**

- **2026-09-01, Nick:** *no refused-ring record* — the island keeps no record of who called
  whom; blocked rings are dropped.
- **2026-08-25, Nick:** sender-anonymity is in scope for friends — the island learns neither
  who is friends with whom nor who is calling, on the ring path.

A `(call_ulid, woken_device_rows, expires_at)` row is a who-rang-whom record with a ~30 second
life and a reaper. Two readings are available and they are genuinely different:

- **Transient operational state is not a record.** The island already holds the device tokens
  and already routes the wake — design 14 §5 says so plainly (*"the island routes the wake,
  therefore it holds the device token, therefore it knows who it woke"*). The lease retains for
  30 seconds a fact the process already has in memory for the duration of the send.
- **A row is a row.** It is on disk, it survives a crash, it is greppable, and it is subpoenable
  in a way an in-flight local variable is not. The ruling did not say *durably*; it said *no
  record*.

**I am not tie-breaking this, and the design does not proceed past it.** Per CLAUDE.md, a
two-source conflict gets surfaced. The cheapest thing that might dissolve it: **the lease does
not need to name recipients.** Pairing the wake charge needs to know *how many* charges were
paired and against which ULID — a count and a deadline may be sufficient, with the device rows
held only in process memory for the send. If that is true, the lease holds no pair at all and
the conflict evaporates. **That is the first thing round 3 should test**, and it is filed
rather than assumed.

### 1.6 The count-only test, RUN — and the lease comes off disk recipient-free

§1.5 named a test: *does the lease need to name recipients at all?* Run against each guarantee
separately, because they do not need the same things.

| guarantee | what it actually needs | recipients? |
|---|---|---|
| **replay-spentness** (Kelvin's ghost call) | has this ULID been seen, and is it settled | **no** — `call_ulid` + `state` |
| **drop a lone `call_end`** (#4325) | does a lease exist for this ULID | **no** — `call_ulid` |
| **pair the wake charge** (#4325) | that the end's charge was already paid | **no** — see below |
| **the ring ceiling** (Nick 2026-09-09) | something to *send* at T+30 | **YES** — and this is the whole of §1.5 |

**Three of four are recipient-free, and the third is free for a reason worth stating.** #4325's
own words are *"a call is one interruption costing two wakes"* — so charge **two at invite
time**, in the recipient's bucket, atomically. An end wake arriving against a live lease is then
free, and the island needs to know only that the lease exists. The end wake's own recipient
comes from the incoming end message, not from the lease. The pairing never reads a stored
recipient; it reads a paid-in-advance reservation. That is #4325's prescription taken literally
rather than approximated.

**The ceiling is the only guarantee that needs the pair**, because at expiry the island must
*send*, and APNs needs a token. Two apparent escapes both fail:

- *Store the channel id instead of the device rows.* Not cheaper — **in a DM the channel IS the
  pair**, which is this repo's own established result. Same fact wearing a channel's clothes.
- *Set the push's APNs TTL to 30s and let Apple drop it.* That genuinely is the island expiring
  its own push with zero state, but it bounds **entry**, not the ring: it does nothing about a
  ring already reported at t=2s. Insufficient for the ceiling as ruled.

### The escape that does work: the recipients never reach disk

§1.5's two readings turned on **process memory versus a row** — *"on disk, survives a crash,
greppable, subpoenable, in a way an in-flight local variable is not."* That distinction is the
answer, not just the framing of the problem:

> **On disk: `(call_ulid, issued_at, expires_at, state)`. Nothing pair-shaped.**
> **In process for the window: the tokens to fire the expiry wake**, held in the scheduled
> expiry task — where they already live for the duration of the send.

The ceiling's recipients are needed for exactly the ~30 seconds the process is already holding
them, and they are held where they already are. Nothing is written that a later reader, a
backup, or a subpoena can recover. **`plan_deliveries`'s table, §1.1, loses its fourth row.**

**What this costs, stated rather than discovered:** a process restart inside a live ring drops
that ring's island-enforced ceiling. The degradation is bounded and already measured — the ring
still ends, at iOS's ~60s rather than our 30s (claude-tasks#4278, n=1 device/OS, so scope it).
The gateway is **single-worker by construction** (`worker_guard`), so there is no second worker
holding a ceiling this one cannot see; if `GATEWAY_ALLOW_MULTIWORKER` is ever set, this
degrades further and that is the tripwire to record alongside the wake budget's identical one.

### 1.7 What is left for Nick, now much narrower

§1.5 asked whether the lease collides with *no refused-ring record* (2026-09-01) and ring-path
sender-anonymity (2026-08-25). After the test, the collision is **not with the lease**. It is
with **the ceiling alone**, and only in process memory.

If that is still too much, there is a real fork and it is a product trade, not an engineering
one — because **the ceiling's first stated reason has already been corrected**. It is no longer
*"nothing stops the ring, so the island must"*; #4278 measured a ring self-expiring at ~60s. It
is now *"it stops at 60s and we want 30s."* So:

> **Is holding who-we-rang in process for 30 seconds the price of a 30-second ring instead of a
> ~60-second one?**

Answer *no* and the ceiling goes back to iOS, the lease becomes recipient-free end to end, and
Nick's two rulings are untouched by anything. Answer *yes* and the lease ships as above.
**Nothing else in this design depends on which way that goes** — which is the point of having
run the test rather than asking the broad question.

---

---

## Part 2 — §0's table, split (temper flaw 6, claude-tasks#4375)

§0 claimed seven open items are *"one absence seen from seven angles."* It is a satisfying
sentence, and the unity is what let the obvious correctness of `room_for_call(ulid)` carry two
expensive and contested changes. Split:

### IDENTITY — the wake names a channel, not a call

| item | |
|---|---|
| claude-tasks#4265 | the end wake names a channel, not a call |
| claude-tasks#4327 | the UUID equality check — *"by construction"* is a theorem about a copier |
| `room_for_call(ulid)` | this call vs the next; rooms that die |

**Justification:** the client already mints the ULID for CallKit. The island simply never sees
it. This is promotion, not invention.

**Blast radius — two irreversibles, not one.** §6 priced only the first:
- the **wire door**: `is_call_invite` is exact equality, so shipping v2 before the island has a
  v2 read path silently stops every ring on both islands with no error anywhere. Island-first,
  compatibility branch;
- the **media plane** (temper flaw 7, unpriced in §6): the room rename lands on token mint, the
  occupancy endpoint, `island_id` namespacing and the app's join path, across two live islands
  whose LiveKit configs are hand-edited and structurally divergent (claude-tasks#3685).

**This is the cheap, uncontested one.** It should be able to ship without waiting on anything
below.

### MEMORY — the lease

| item | |
|---|---|
| claude-tasks#4325 | pair the wake charge, or refuse a lone `call_end` |
| the island-owned ring ceiling | Nick 2026-09-09 |
| replay-spentness | Kelvin's ghost call |

**Justification:** Part 1. **Blast radius:** new state, a reaper, two writers, cage-match by
law — and §1.5's unresolved collision with two of Nick's rulings, which gates it.

### ROUTING — the invite stops being a channel message

| item | |
|---|---|
| §4 | the invite as a first-class wire object rather than a message with a magic body |

**Justification:** a call is ephemeral and its permanence in channel history was inherited from
the transport, never chosen.

**Blast radius: the largest of the three, and §4 already flags itself as the least trustworthy
part of the document.** Temper flaw 2 (Tesla, alone) adds the cost §4 missed: it **severs the
federation plane**, because a cross-island call reaches the far island over the message path.
Two delivery paths (socket, wake) must then agree on one object — the silent-desync class this
repo has been bitten by.

### The correction this split forces — and M4's own condition has since FIRED

12a temper flaw 4 (*three clocks unowned*) sat in §0's table as evidence, and §3 claimed all
four collapse into `invite.expires_at`. Temper flaw 6 struck that, citing `12a-MEASURED` M4.
**Re-reading M4 rather than restating the strike changes the answer**, so both are recorded:

| clock | caused by the absence? | |
|---|---|---|
| ring ceiling, 30s | **yes** | it is the lease's `expires_at`, and it is unowned precisely because nothing owns a lifetime |
| APNs expiration, 60s | **NO LONGER SETTLED — see below** | M4 called the 60s/10s split *"neither is wrong, they belong to different architectures"* — **conditionally** |
| client freshness, 10s | **NO LONGER SETTLED** | same condition |
| room lifetime (SFU `empty_timeout`) | **no** | §3's fourth row was already correct: the invite bounds ENTRY, not duration |

**M4 did not rule the divergence permanent. It scoped it and named its own expiry:**

> *"The collapse is correct only under CallKit, and the system is not running CallKit."*
> **TEMPER.md flaw 4 amended:** the collapse is conditional on the CallKit transition, and the
> alert-world rationale must be explicitly retired at that point rather than silently
> contradicted.

**The CallKit transition has happened.** VoIP shipped in v0.11.0 (2026-09-11) and a real locked
handset rang through CallKit on 2026-09-14. M4's condition fired between the strike (2026-09-13)
and this document — so Maxwell's flaw-6 citation was *correct at strike time* and is now stale in
the reassuring direction: it reads as *"flaw 4 is settled, leave it"*, which is the exact disposal
M4 wrote an expiry clause to prevent.

**What is actually true, stated at its real scope.** Under the alert transport a late wake is
still useful — the user taps it and enters a room whose door is open. Under CallKit a late wake
**reports a call**, and a report arriving after the ceiling expired is a ring for a call that is
over. The 60s constant and the ring ceiling therefore *do* interact under CallKit, in the
direction M4 predicted. That does not make the collapse correct — it makes it **re-openable and
currently undecided**, which is a third status, distinct from both §0's *"caused by the absence"*
and flaw 6's *"not caused by the absence"*.

**So: one of four caused, one not caused, two re-opened.** Folding all four into "one absence" was
still the escalation-under-reuse pattern §0 committed — but the correction is not the one the
strike prescribed, and the difference was only visible by **opening M4 instead of quoting the
sentence that quoted it**. §3's table is retracted pending that decision, which is owed.

---

## Status after this recast

| flaw | disposition |
|---|---|
| 1 — statelessness is false; the state is a lease | **DISCHARGED** by Part 1. §2b retracted; the lease named and priced; **blocked at §1.5 pending Nick** |
| 2 — §4 severs federation | **CROSS-TAB, open** — claude-tasks#4374 |
| 5 — co-presence unbounded and TOCTOU-racy | **MOOT for now.** The lease is not an ACL; co-presence leaves the 1:1 path entirely and waits for gatherings (claude-tasks#4387) |
| 6 — §0 over-unifies | **DISCHARGED** by Part 2, including the flaw-4 retraction |
| 7 — room rename blast radius | **PRICED** in Part 2 under IDENTITY; still a separate irreversible |
| 8 — the join door is unnamed | **OPEN.** Not touched here — claude-tasks#4387 |

### Owed out of this document

| item | owner | |
|---|---|---|
| ~~§1.5 — does a ~30s lease collide with the two rulings?~~ | — | **NARROWED by §1.6/§1.7** — not the lease, the ceiling, and only in process |
| ~~Test whether the lease can hold a **count**~~ | island | **DONE — §1.6.** 3 of 4 guarantees are recipient-free; the ceiling alone needs the pair, and only in process memory |
| **Nick's remaining call, narrowed:** is holding who-we-rang in process for ~30s the price of a 30s ring instead of iOS's measured ~60s? | **Nick** | nothing else depends on it |
| 12a flaw 4 re-opened: M4's CallKit condition has fired, so the 60s/10s divergence needs re-deciding and the alert-world rationale in `apns.py` explicitly retired | island | adjacent to #4382, #4265 |
| §2b retracted; §3's first rows retracted — design 14 itself is NOT edited, per the convention that a design of record is answered rather than rewritten | — | done here |

**Nothing here is decided and nothing is built.** Design 14's own §7 question 3 asked whether
`invite.expires_at` is actually stateless *"or does enforcing it require the island to remember
which invites it issued — which is the row again."* The answer is yes, it is the row again, and
the document was right to ask.
