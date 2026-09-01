# 🜂 Temper — the cold pole

*Movement 6. Cross-family adversarial strike on the DESIGN (not code). The whole
bundle was handed over — including `CRUCIBLE.md`'s enthusiasm — so a laundered
assumption could be caught at its source.*

## Verdict: **CANDIDATE INVALIDATED** — 2 of 4 families DISSOLVE

| Reviewer | Family | Verdict |
|---|---|---|
| **Carnot** | GPT / Codex | **DISSOLVE** |
| **Tesla** | xAI Grok | **DISSOLVE** |
| **Kelvin** | Gemini 3 Pro | **RECAST** |
| **Maxwell** | Claude (author) | **concur with DISSOLVE** after verifying Tesla's citations |

The skill's binding rule fires: **≥2 families DISSOLVE ⇒ stop and report the candidate
invalidated.** That is an honest negative result, and it is recorded rather than
softened.

> ### ⚠️ Author error, corrected — I nearly published the wrong verdict
>
> An earlier draft of this file recorded **RECAST**, with Tesla as a dark seat and an
> "instrument failure" note citing task #3503. **That was wrong.** Tesla had not
> stalled; it was still running. I spot-checked its output file at ~60 words, saw
> preamble, and declared it dead. It finished minutes later with **919 words and the
> sharpest strike of the three** — including two findings that neither other family nor
> my own Fold caught, and that invert the verdict.
>
> I had already written the wrong call into `TEMPER.md`, `BLADE.md`, PR#155, a new
> tracker issue (#3805) and a comment on #3503 before it landed. All are corrected.
>
> **The lesson is not "Grok is slow."** It is that *I manufactured a false negative by
> measuring too early, and the reading I got was one I had a motive to accept* — a dark
> seat is a shorter path to a finished verdict than a live one. A wait-for-completion
> loop, not a spot check, is the correct instrument, and the memory's own warning that
> availability and value run **anti-correlated** across reviewer seats was sitting right
> there.

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

## Second finding: Tier 1 is a real DEFECT, but the fix as cast is refuted

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

### ~~The C-5 answer~~ — **SUPERSEDED AND WRONG, kept for the record (see T-B below)**

> **This subsection is retained deliberately rather than deleted.** Everything in it is
> wrong, it was written with confidence, and it is the clearest artifact in this bundle of
> the exact failure the run's headline finding is about: I read a *doc comment* about
> `client_msg_id` and reported it as the *generator*, then built a design refinement on
> it. Tesla caught it. Deleting it would hide the instance.


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

## Tesla's strike — the two findings that inverted the verdict

Both were verified in the code by me before being accepted. **Both hold.**

### T-A — the fix does not stop the attack it was written for

> *"Origin signs those content fields plus `client_msg_id`, **not** `msg_id` or
> `created_at`… Echoing the row's `client_msg_id` catches a sloppy transplant that
> forgets to copy the stored id. A motivated operator copies `client_msg_id` with the
> origin, drops the old row, inserts a new ULID. Uniqueness holds. Echo matches.
> Position moved. Undetected."*

And the constraint I leaned on in Fold F-4 is not a boundary at all against this
adversary: *"The `UniqueConstraint` … is not a security boundary on a box the operator
owns. ISL-0002: FK off, they have the DB."* Correct — I used a database invariant as a
defence against the party who administers the database.

Plus a client half I never specified: `validateOrigin` currently feeds
`origin.client_msg_id` back in *as* the frame id (`origin_envelope.dart:243-251`), so
the comparison stays self-referential until the app compares the **outer** field and
fails closed. *"Echo is not the capability."*

### T-B — C-5 is wrong, and this repo already recorded the right answer

I claimed `client_msg_id` is a ULID, so echoing it disclosed only a timestamp already
published inside `origin`. **False, and I got it from a doc comment rather than the
code.** The app generates a **UUIDv4** — `chat_providers.dart:566`,
`newTempId: () => _uuid.v4()`. I had read `redacting_log_sink.dart:30`, a
*log-redaction* comment, and treated it as the generator.

And the island's own account-deletion path already classifies this column as PII —
`accounts_service.py:157-159`, verbatim:

> *"`client_msg_id` — a 64-char client-supplied string (validated only as \"a string\")
> that can hold an email/phone/handle. **Naming a column `*_id` does not make
> attacker-controlled input non-PII.**"*

Deletion **nulls this field specifically because it carries the person**. My design
proposed broadcasting it to every channel member on every read path forever. Those two
positions cannot both stand and the deletion code's is right.

Tesla also names the enthusiasm tell precisely: Fold *celebrated* that the fix would
help unsigned traffic too (F-4), when unsigned traffic is exactly where the echo is
**pure new leakage with no security benefit**. *"That is enthusiasm smuggling."*

### T-C — what Fold missed

- The ack is **WS-only** (`ws.py:225`); REST and bus-born messages get no receipt at all.
- **Reactions carry the same origin/cmid tautology** and go unmentioned in the design.
- A signed ack would inherit the `island_pubkey` TOFU problem: verifying a manifest
  proves it is self-consistent, *"NOT … the island you meant to reach"*
  (`island_identity.py`), and the app deliberately does not verify the manifest it
  already fetches.
- `origin.client_msg_id` **survives account deletion** (origin is kept; the row's column
  is nulled) — so the PII tombstone is already leakier than the deletion code intends.

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

- **T-1 — the signed ack is dead for now, not forever.** Owner: Nick. Cost: no conviction
  story exists. Rationale: a voluntary proof is theatre against an operator who is the
  adversary; the known fix is **client-side refusal** (how Chrome forced CAs to log), and
  it is meaningless while one person runs every island. **Revisit trigger: the first
  non-Nick island operator** — the same gate #3387 waits on.
- **T-2 — the position-binding tautology stays OPEN and unowned.** Owner: unassigned.
  Cost: a dishonest island can relocate a validly-signed origin and no reader can tell —
  the app tab's "reason 2", still unfixed. Rationale: the obvious fix (echo the row's
  `client_msg_id`) **publishes a field this repo's own deletion path tombstones as PII**
  and does not defeat a motivated operator anyway. A real fix must bind position *without*
  broadcasting free-text client input. Recorded on #3805, re-scoped from task to finding.
- **T-3 — `client_msg_id` is unconstrained free text on the wire contract.** Owner: app
  tab. Cost: `accounts_service.py:157` already treats it as PII, and `origin.client_msg_id`
  **survives account deletion** while the row's column is nulled — so the tombstone is
  already leakier than the deletion code intends. That is a live finding independent of
  this whole design, and arguably the most actionable thing the strike surfaced.

## Instrument note (corrected)

Tesla produced **0 bytes** against the *Spark* seed (~3.5KB, generative, timed out at
400s) — a real failure, and the one genuine data point for claude-tasks#3503. But it
**completed the Temper strike** (~75KB, ~15 min, 919 words). That inverts the
size hypothesis rather than supporting it: the large agentic task succeeded and the
small pure-generation one did not. #3503 has been corrected accordingly.

**The dark seat in this run was mine, not Grok's.** See the verdict header.