# 🜂 Temper — the cold pole

*Movement 6. Cross-family adversarial strike on the DESIGN (not code). The whole
bundle was handed over — including `CRUCIBLE.md`'s enthusiasm — so a laundered
assumption could be caught at its source.*

## Verdict: **RECAST — Tiers 0 and 1 survive and ship; Tiers 2 and 3 are HALTED**

| Reviewer | Family | Verdict |
|---|---|---|
| **Carnot** | GPT / Codex | **DISSOLVE** |
| **Kelvin** | Gemini 3 Pro | **RECAST** |
| **Tesla** | xAI Grok | **NO VERDICT — instrument failure** |
| **Maxwell** | Claude (author) | **RECAST — concur, with one dissent from Carnot** |

Not auto-invalidated (that needs ≥2 DISSOLVE), but **the two live families converge
completely on what dies and what lives**, and they got there independently.

---

## The convergent fatal finding: the mechanism defends against an adversary who does not exist

Both reviewers went straight to §3's admitted limit and both concluded it is not a
limit, it is a hole in the floor.

> **Carnot:** *"Since the operator is explicitly the adversary, the adversary simply
> runs old code, strips signatures, or signs only benign cases. A mechanism that only
> convicts an upgraded operator who voluntarily keeps incriminating output enabled is
> not adversarial auditability."*

> **Kelvin:** *"The mechanism defends against an operator who is both malicious enough
> to tamper with message history but also compliant enough to opt-in to a system that
> creates signed, self-incriminating evidence of that tampering. This is not a real
> adversary… A voluntary proof is theatre."*

And both traced it to the same authorial mistake, which is the one that stings:

> **Kelvin:** *"The Spark movement found the answer in Kelvin's 'Receipt Weaving': a
> proof that is load-bearing for the island's own operation. The design discarded this
> property because of the privacy leak from a public log — but in doing so, it threw
> away the key to the entire mechanism."*

> **Carnot:** *"'sign the ack' is not the smaller true answer; it is what remained after
> discarding the only load-bearing property from Kelvin's spark."*

**They are right.** Fold caught this as F-3 and I carried it as a stated limit instead of
treating it as disqualifying. Naming a flaw openly is not the same as pricing it, and I
priced it wrong: I filed "cannot be compelled" as a *scope note* when it is the property
the entire conviction story rests on.

### Maxwell's dissent from Carnot's DISSOLVE, recorded because it changes the recast

Carnot says do not build it. I think that overshoots by one step, and the counter-example
is **inside our own research bundle**: CT faced this exact problem — a CA cannot be
*compelled* to log — and solved it not with cryptography but with **client-side refusal**.
Chrome requires SCTs; that requirement, and nothing else, is what forced CAs to log. The
proof became load-bearing because *the client would not proceed without it*.

Kelvin reached the same door independently: *"This could mean client-side enforcement
(clients refusing to process further actions without valid signed acks)."*

So the missing component is identified and precedented. But it lands the design somewhere
honest and unflattering: **client-side enforcement is only meaningful against an operator
Nick does not control, and there are none.** With two islands both run by Nick, a
"requirement" he imposes on himself proves nothing. The signed ack is therefore not slag —
it is **premature**, and its natural gate is the arrival of a third-party operator, which
is the same gate #3387 already waits on.

That is a real difference from DISSOLVE: *do not build it now*, versus *do not build it*.

---

## Second convergent finding: Tier 1 is a defect repair, must be SEVERED, and is blocked on a privacy answer

Both reviewers independently upgraded Tier 1 and independently refused to let it ship as
written.

> **Kelvin:** *"This is not a feature, it is a defect repair. It should not be bundled
> with the speculative work in other tiers."*

> **Carnot:** *"That is actionable. But it is just message integrity plumbing, not
> reputation dissolution."*

And both hit C-5 hard, correctly:

> **Kelvin:** *"'Probably fine' is not a substitute for a security review… the client
> specification must be updated to mandate that `client_msg_id` be a high-entropy,
> non-correlatable value."*

> **Carnot:** *"A client-chosen opaque ID can contain UUIDs stable across chats,
> device/session prefixes, timestamps, counters, database IDs, or library fingerprints…
> Tier 1 may fix integrity by expanding metadata leakage."*

### The C-5 answer, measured after the strike was launched — and it improves the fix

I checked rather than conceding, and the picture is better than either reviewer assumed,
in a way that yields a cleaner design than the one I cast:

1. **The app's `client_msg_id` is a ULID** — `aiko_chat_app/lib/core/logging/redacting_log_sink.dart:30`
   describes it as *"26 chars, Crockford base32."* A ULID embeds a 48-bit millisecond
   timestamp, so it discloses **client-side composition time**, distinct from the
   island's `created_at`. That is a real disclosure and Carnot's instinct was sound.
2. **But it is already published for exactly the messages that matter.** The stored
   `origin` object *already contains* `client_msg_id` (`models.py:549`) and
   `message_view` already echoes `origin` wholesale (`:57-58`). **For signed messages the
   value is on the wire today.**

Which resolves the tension instead of trading it off:

> **Echo the row's `client_msg_id` ONLY when `origin` is present.**

- Zero marginal disclosure — the value is already there, inside the signed envelope.
- Exactly the messages where the comparison has security value (an unsigned message has
  no signature to relocate).
- Preserves the codebase's established omit-when-empty contract, the same one `origin`
  and `mentions` already use.

**Carnot's wider point still stands and is carried as a named tradeoff:** the wire
contract does not *constrain* what a client puts in that field, and a different client
implementation could put something correlatable there. That is a spec rule owed to the
app tab (V-6 below), not a blocker on the island-side fix under the origin-present
scoping.

---

## Third convergent finding: Tier 0 is not an evasion

Both reviewers, unprompted and emphatically:

> **Kelvin:** *"the most valuable finding in the entire bundle… Shipping Tier 0 unblocks
> a stakeholder and a two-month-old ticket for the cost of one conversation. That is
> infinite leverage."*

> **Carnot:** *"Tier 0 is not evasion… Putting that first is a real process finding. It
> just is not part of the engineering design."*

Carnot's qualifier is the honest one and is adopted: it is a real finding **and** it does
not belong inside an engineering design doc. It is routed to a task, not to a build step.

---

## Was the reframe honest, or a retreat dressed as rigor?

Split, and the split is informative.

> **Kelvin:** *"It is not a retreat dressed as rigor… a textbook example of a crucible
> doing its job. The problem is not the process, but the destination… The honest answer
> was small, but it might also be useless."*

> **Carnot:** *"The reframe is mostly honest, but the final proposal retreats past
> usefulness… The design should have stopped at 'KT later; fix position binding now.'"*

Both accepted the *process* and rejected the *destination*. Carnot's sentence is the one
worth keeping, because it names what the design should have been — and it is almost
exactly what this Temper leaves standing. **The forge overshot by inventing Tier 2 to
have something to build.** That is the ship-momentum failure with a design doc instead of
a PR, and neither the enthusiasm case nor Fold caught it; it took an outside family.

---

## Named tradeoffs (owner + accepted cost + rationale)

- **T-1 — the signed ack is deferred, not abandoned.** Owner: Nick. Cost: the conviction
  story does not exist until a third-party operator does. Rationale: client-side
  enforcement is the known fix (CT precedent) and is meaningless while one person runs
  every island. **Revisit trigger: the first non-Nick island operator** — the same gate
  #3387 waits on.
- **T-2 — `client_msg_id` remains client-chosen and unconstrained by the wire contract.**
  Owner: app tab. Cost: a non-conforming client could place correlatable data in a field
  the island now echoes. Mitigation: origin-present scoping bounds it to values already
  published; a spec rule is owed.
- **T-3 — Tesla's seat was dark.** Cost: three of four families struck, not four.
  Rationale: recorded, not papered over — and the availability/value anti-correlation
  warning in memory says a dark seat is not a neutral one.

## Instrument failure (recorded, not hidden)

Tesla (Grok) answered a trivial liveness prompt with `PONG` but produced **no verdict**
against either the Spark seed (0 bytes) or the Temper bundle (60 words of preamble, then
an agentic loop that never emitted). This is claude-tasks task **#5** reproducing twice in
one run, and the second instance disproves any residual "it is just size" reading — it
entered the loop after successfully reading the bundle. No verdict was fabricated for it.
