# TEMPER.md — island design 14, "the call is a WIRE object"

**Overall verdict: RECAST** (4/4 families struck, **4/4 RECAST, zero DISSOLVE**)
**Struck:** `dt-1789175551`, 2026-09-12, against `14-the-call-is-a-wire-object.md` @ the commit
that introduced it.
**Families seated:** Maxwell (Claude) + Kelvin (Gemini 2.5 Pro) + Carnot (GPT/Codex) + Tesla
(Grok) — **4/4**. Wu (Kimi K3) disabled upstream.
**Bundle:** island 14 + `12a-RECAST.md` (33.8KB).
Raw strikes, deliberately unedited: [`temper-strikes-14/`](temper-strikes-14/).

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Maxwell (Claude) | RECAST | The stateless ACL works only for a set FIXED AT CREATION, which is not a gathering — so it may support only the 1:1 case it was written to move past. |
| Kelvin (Gemini) | RECAST | Statelessness is a thermodynamic fiction: without a spent-ULID memory, an unexpired invite for a call that is *over* resurrects it as a ghost call. |
| Carnot (GPT) | RECAST | The core move is right and the candidate overclaims statelessness while underpricing the new wire object. *"Do not let 'not a database row' become theology."* |
| Tesla (Grok) | RECAST | Identity, lease and transport are three objects wearing one body; you reopened #3170 on a gathering you will not ship, and §4 cuts the bus that carries a cross-island call. |

---

## Fatal flaws (deduped, most severe first)

### 1. The statelessness claim is FALSE — and the state is a LEASE, not an ACL. (4/4)

Four families, four independent routes, one conclusion.

- **Maxwell:** a growing participant set needs ordering; two signed invites naming different sets
  for one ULID and the island cannot tell which is later. Accept any superset ⇒ removal becomes
  impossible (no mid-call ejection, ever). Reject amendments ⇒ the set is fixed at creation,
  which is not a gathering.
- **Kelvin:** replay. The island must remember every processed ULID until `expires_at` or a valid
  unexpired invite for a finished call re-wakes every participant. *"The dissolved row #3170
  returning from the void, not as decorative slag but as a load-bearing heat sink."*
- **Carnot:** enumerates the remembered facts the guarantees imply — was an invite sent, is this
  end lone, was a charge paired, has this replay been consumed — *"per-call state by another name
  unless each guarantee is deliberately weakened."*
- **Tesla, and this is the sharpest cut in the strike:** §2b answered temper flaw 10 **with the
  wrong function.** The dissolved row was a **LEASE** — pairing wakes, dropping lone ends, firing
  a stop. `may_be_in = f(set, policy, occupancy)` is a **join ACL**, and co-presence only has
  content at `n≥3`, which §5 of this same document explicitly declines to ship (#2731, #3196).
  *"You have reopened #3170 on a gathering you will not ship, which is exactly 'the decorative
  object wearing a function's clothes' you invited the temper to test."*

**The document set that trap in §6.5 and then walked into it.**

**DISPOSITION: fold.** Name the **lease** and price it against #3170's DISSOLVE reasoning —
that is what temper flaw 10 asked for and what §2b claimed to have escaped. If the lease is
refused, then **explicitly drop** lone-end suppression (#4325) and the island-enforced ceiling,
and state that Nick's ceiling ruling is device-best-effort until old builds die. What is not
available is claiming both.

**The silver lining, and it is real:** #3170's bar for reopening was *"name a server-side
decision that must be authoritative."* This design tried to clear it by arguing no state is
needed; four families showed state **is** needed and is **load-bearing**, which is the bar. The
design fails its own claim and clears the original gate in the same move — but the authoritative
object is the lease, not the ACL.

### 2. §4 severs federation — an entire plane, unpriced. (Tesla, alone)

> *"VoIP replaced island → **this island's** devices. It did not replace island → **the other
> island**. Today's channel message is how a callee-hosted 1:1 (2026-08-30, in scope) carries a
> signed artifact to the island that can actually wake the handset. Pull the invite off that bus
> and the same-island demo rings while the household topology goes silent — no error anywhere,
> the sibling of your v2 one-way door."*

Two further consequences the document never counted:

- **Deleting the channel message deletes `should_wake(kind == "dm")`** — the live DM-only
  scaffold — **before #4326 re-derives interruption policy.** That is how a stranger lands on
  VoIP *by accident of the new object*.
- **A trilemma, newly named:** *statelessness, locked-phone join, and a sealed envelope — pick
  two.* If the full readable invite must ride the VoIP payload so a locked phone can join with
  no island row, you have handed **design 20's adversary (Apple as reader) a participant set.**
  §4 pretends to hold all three.

**DISPOSITION: fold.** Keep v2 **in-band** (`aiko:call/2 <ulid> …` on the existing dual-island
history bus, same object on the wake). A first-class event type may share that bus later; it may
not assume VoIP did the job. **Do not delete `kind == "dm"` until #4326 exists.**

### 3. §4's premise is unjustified: permanence RELOCATES, it does not vanish. (4/4)

- **Maxwell:** the document never argues that permanence is a *defect*. A call log is something
  every telephone has had for a century. The frame omits **message AND call-id**, which keeps the
  record and still fixes #4265. Citing the no-refused-ring-record ruling is an axis-upgrade: that
  ruling is about **blocked** rings, not calls that happened.
- **Kelvin:** *"This isn't a removal of permanence, it's a relocation, and the design doesn't pay
  the transport costs."*
- **Carnot:** it relocates into *"APNs logs, socket events, client local state, missed-call UI,
  analytics, retry queues, reconciliation paths."* The choice was never message-or-no-record; it
  is **which projections exist, with which retention and authority.**

**DISPOSITION: fold.** Add the third option to the frame and let it compete; justify the premise
or drop it.

### 4. "Setting is not owning" — the ceiling fused to entry, then handed to the device. (3/4)

- **Maxwell:** nothing makes the device honour `expires_at`; an older build ignores an unknown
  field. Flaw 3 is **relocated** (island stops a live call → client never stops a dead ring),
  not removed, and the document claims removal.
- **Tesla:** *"Three ownership claims, one quantity, zero protocol for answer. That is not a
  derivation table. That is three clocks arguing in a coat."* Void-on-answer is a local story
  about **one** handset; the signed field does not change, so the island still refuses the second
  phone, and an old build never voids.

**DISPOSITION: fold.** Uncollapse the **ring window** from the **join bearer**. Write the
reconnect / second-device / mid-call-drop / old-build rows now, not after the first hour-long
call.

### 5. Co-presence is unbounded, unnamed, and TOCTOU-racy. (3/4)

- **Kelvin:** `n(n−1)/2` — 4,950 lookups at n=100 — a DoS vector with no specified failure mode.
  Does it fail open or closed?
- **Carnot:** the document never names **which operation** must be authoritative (invite mint?
  wake? token mint? SFU join? ongoing occupancy?). *"Until the gate is named, the argument is a
  function wearing a server-shaped hat."*
- **Tesla:** occupancy is a moving observation, not a transaction boundary. B and C join
  concurrently, each snapshot misses the other, the induced subgraph goes unexamined, and *"at
  3am the room contains two people who refused each other."* Needs a per-ULID join lock (state)
  or an SFU allowlist (*"state, just not yours"*).

**DISPOSITION: fold.** Keep occupancy **off the 1:1 hot path**. Co-presence waits for gatherings;
if it returns, serialize the join or push the allowlist into the SFU, fail closed on occupancy
loss, and cage-match that surface.

### 6. §0's table over-unifies — three objects wearing one body. (2/4, and structural)

- **Tesla:** identity (#4265, #4327, `room_for_call`) / **memory** (#4325 pairing, the
  island-owned ceiling, replay-spentness) / **routing** (leaving the channel) are three distinct
  objects. The table's unity lets *"the obvious correctness of `room_for_call(ulid)` smuggle a
  #3170 reopen and a transport cut."*
- **Maxwell:** temper flaw 4 is **not** caused by the absence — `12a-MEASURED` M4 established the
  60s/10s divergence as a deliberate, reasoned architectural choice. Folded into "one absence"
  because it made the case tidier: the escalation-under-reuse pattern, committed inside the
  document that documents it.

**DISPOSITION: fold.** Split the table into the three objects; each gets its own justification
and its own blast radius.

### 7. The room rename's blast radius is unpriced. (Maxwell)

`room_for_call(ulid)` replacing `room_for_channel(channel.id)` is a **media-plane** change: token
mint, occupancy endpoint, `island_id` namespacing, the app's join path — landing on two live
islands whose LiveKit configs are hand-edited and structurally divergent (#3685). §6 prices only
the wire door. **DISPOSITION: fold** as a separate irreversible.

### 8. The join door is unnamed. (Tesla)

Who mints, who signs, who may submit a wake, whether the set is a proposal or a bound, whether
Bob can replay Alice's invite to wake *her* — none of it is a sequence. *"A wire object with no
protocol is a noun."* **DISPOSITION: fold** — write the sequence table (mint, wake, join, answer,
end, replay, reconnect, second device, old build).

---

## What holds

- **§0's diagnosis is the ore, 4/4.** *A magic string spoken into a channel is not a call*, and
  `room = channel.id` is why "this call" and "the next call" are the same room forever. Promote
  the client ULID, key the room by it, put it on the wake. Every family kept this.
- **Unfusing wake / co-presence / occupancy as QUESTIONS** is right (3/4) — even though
  co-presence is not yet a shippable gate.
- **Identity on the wire + policy re-read fresh at every island gate is the correct revocation
  shape**, and it is *not* 12a's arm (C) (3/4). Tesla adds: the two-cell result of `12a-RECAST`
  is not regressed **so long as the invite stays readable to the island**.
- **Bounding APNs TTL by the ENTRY ticket while keeping SFU `empty_timeout` as call duration** is
  the one honest split in §3 — and the alert-world "60s deliberately longer" rationale should be
  retired out loud.
- **§5's negative space, the v2 one-way-door warning, and the author's own brand on §4 as the
  least-trusted page.** Carnot: *"That humility is not decoration; it is load-bearing engineering
  judgment."* Tesla: *"The age of the confidence is the tell; believe the brand, not the reach."*
  §4 was flagged as least trustworthy and was struck 4/4 — **the self-flag was calibrated.**

---

## Disposition

**RECAST.** Round 1 of ≤3. No family voted DISSOLVE, so the candidate is not slag — but flaws 1
and 2 are load-bearing, and flaw 2 is **cross-repo** (it lands on the app tab's design 20 as hard
as on this document, via the sealed-envelope trilemma).

Before round 2 — **FILED, because a list in a design doc is not where open work is looked for
in this repo** (open work lives in `claude-tasks` under `project:aiko-chat-island`; a
"before round 2" list in git is readable but mis-homed, which is worse than hidden because it
looks filed):

1. **Name the lease** and price it against #3170, or explicitly drop what it was buying —
   [#4373](https://github.com/nickmeinhold/claude-tasks/issues/4373)
2. **Hand flaw 2's trilemma to the app tab** — *statelessness / locked-phone join / sealed
   envelope, pick two* — it strikes design 20's adversary model directly —
   [#4374](https://github.com/nickmeinhold/claude-tasks/issues/4374)
3. **Do not delete `kind == "dm"`** before #4326 exists. It is the live interruption-policy gate.
   Recorded as a precondition comment on
   [#4326](https://github.com/nickmeinhold/claude-tasks/issues/4326).
4. **Split §0's table** into identity / memory / routing before anything is built on it —
   [#4375](https://github.com/nickmeinhold/claude-tasks/issues/4375)

**Nothing in design 14 is decided, and nothing is built.**
