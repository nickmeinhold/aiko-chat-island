# 🜂 Blade — an honest negative result

*Movement 7. **CANDIDATE INVALIDATED** (2 of 4 families DISSOLVE). There is no blade:
the forge produced a plan that did not survive its own fire, which is the outcome this
skill exists to be capable of reaching. Plan mode was not entered — there is nothing to
approve. **No code was written.***

## What to do

### 1. Send Andy the answer to his own question — the only unambiguous action

claude-tasks **#2161**, steps 3–4, outstanding since **2026-07-17**. Andy raised the
"softened CA" question; the answer is written and sourced in `docs/design/06` where he
cannot see it. **All three adversaries independently called this the most valuable item
in the bundle** — Tesla, characteristically, adding *"it does not rescue Tiers 1–3."*

Nick's to send, through the draft gate: I draft the Discussions post from Design 06, he
edits, then it posts. It should carry Design 06's own honest limit (*"'No central point'
is not a shipped outcome"*), not the stronger claim, because Andy will find it otherwise.

### 2. Carry one finding that is real and independent of this whole design

`origin.client_msg_id` **survives account deletion** while the row's column is nulled
(`accounts_service.py:157-159` tombstones the column precisely because it *"can hold an
email/phone/handle"*). The tombstone is therefore leakier than the deletion code intends.
This has nothing to do with auditability, was surfaced by the strike, and is arguably the
most actionable thing the night produced. Not yet filed — it wants its own verification
pass first, and I would rather file it right than fast.

### 3. Build nothing else from this bundle

- **Signed ack + prober — dead for now.** A voluntary proof is theatre against an
  operator who is the adversary. The fix is known (client-side refusal, how Chrome forced
  CAs to log) and meaningless while Nick runs every island. Trigger: first non-Nick
  operator (#3387).
- **Echo the row's `client_msg_id` — do NOT build as specified** (#3805, re-scoped from
  task to finding). It broadcasts a field the deletion path treats as PII, and it does not
  defeat a motivated operator anyway, because `origin` signs `client_msg_id` but not
  `msg_id`.
- **Design 06's KT log stays the long-run answer**, gated on five unbuilt things across
  two repos — and it must ship with an honest statement that **N=2 has no split-view
  protection**.

## What this run is actually worth

Nothing was built and one thing was un-learned, which is a real return on a night:

1. **The thesis was refuted twice** — externally (every deployed transparency system kept
   an admission layer; N=2 gossip is a single edge) and internally (**Design 06 already
   held a more careful version of this idea and we talked past it**).
2. **Nick's by-product died on its own falsifier**, written before the research and fired
   exactly as written: age-of-attested-history requires comparing islands, a comparison is
   a ranking, a ranking is the reputation system #1569 already refuted.
3. **A three-family strike caught what I could not.** Kelvin and Carnot found the design's
   fatal property; Tesla found that my *fix* was both ineffective and harmful, using two
   citations I had not read.

## What I got wrong, which is the transferable part

1. **I called the verdict before the last adversary finished, and the reading I accepted
   was the one I had a motive to accept.** A dark seat is a shorter path to a finished
   verdict than a live one. I spot-checked instead of waiting for completion, and wrote
   the wrong call into five artifacts before it landed.
2. **I read a doc comment and reported it as the code.** `redacting_log_sink.dart:30`
   describes `client_msg_id` as a ULID; the generator is `_uuid.v4()`. I then built a
   design refinement on the wrong fact and published it to the tracker.
3. **I filed a disqualifying flaw as a scope note.** Fold found *"an island cannot be made
   to sign"* and I carried it as a stated limit. Naming a flaw is not pricing it.
4. **I invented a tier to have something to build.** Tesla: *"the author could not let a
   crucible end at a message to Andy plus a one-field tautology."* That is exactly what
   happened, and it should have ended there.
5. **The thesis itself was a regression of a better position this repo already held** —
   last session's crux recurring at the level of a thesis rather than a file path.

Points 2 and 5 are the same failure at different scales, and point 1 is its cousin: three
times in one night I trusted a reading I had not earned. **The forge caught all three,
but only because the cold pole was real.**

## Scope stamp

**DESIGN-ONLY. IMPLEMENTATION UNPROVEN. CANDIDATE INVALIDATED.** Do not build from this
bundle without re-reading `TEMPER.md`.
