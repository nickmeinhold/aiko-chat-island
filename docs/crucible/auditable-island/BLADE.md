# 🜂 Blade — what to actually do

*Movement 7. Plan mode was not entered: Nick was asleep for this run and a plan he
cannot approve is not a plan. Presented as prose per the skill's fallback. **Nothing
below has been built.***

## The one-paragraph version

The thesis Nick and I landed on last session — *auditability by construction dissolves
reputation* — **is refuted**, from outside (every deployed transparency system kept an
admission layer; at N=2 split-view detection is theatre) and from inside (`docs/design/06`
already held a more careful version of the same idea and we talked past it). The
incumbent answer, Design 06's KT log, is **right and currently unbuildable** — five gates,
in two repos. What the forge then invented to have something to build (a signed ack) was
struck down by both live adversaries as defending against an adversary who does not exist.
**What survives is one real defect repair, one overdue conversation, and a deferral with a
named trigger.** That is a smaller result than the run set out for, and it is the honest one.

---

## Do now

### 1. Tier 0 — send Andy the answer to his own question *(no code, highest value/hour)*

claude-tasks **#2161** steps 3–4 have been outstanding since **2026-07-17**: *show Nick,
then post to `aiko_chat` Discussions*. Andy raised the "softened CA" question; the answer
is written and sourced in Design 06 where he cannot see it. Both adversaries independently
called this the most valuable item in the bundle.

**This is Nick's to send, not mine** — and it goes through the draft gate first.
Concretely: I draft the Discussions post from Design 06, show him, he edits, then it posts.

### 2. Tier 1 — echo the row's `client_msg_id`, scoped to origin-present *(a defect repair)*

One field in `messages_service.message_view()`, emitted **only when `origin` is present.**

- **Why it is a defect:** the write path enforces `origin.client_msg_id == frame.client_msg_id`
  fail-closed (`signing.py:297`) and the column is `UNIQUE(channel_id, client_msg_id)`
  (`models.py:531`), but the read path never echoes the *row's* value — so the signed
  envelope only ever agrees with **itself**, and a dishonest island can attach a validly
  signed origin to a different row undetectably. The app tab measured this and nobody
  owned the fix.
- **Why origin-present scoping:** the value is *already* on the wire inside `origin`
  (`models.py:549`, echoed at `message_view:57-58`), so there is **zero marginal
  disclosure** — which answers both adversaries' privacy objection instead of trading
  against it. And an unsigned message has no signature to relocate, so the field would be
  pure leakage there.
- **Ship it standalone.** Both reviewers were explicit: sever it from the speculative
  work. It is a bug fix, not a feature.
- **Gates:** wire-visible ⇒ app-tab agreement **before** merge, island deploys first
  (silent-desync rule). Trust-boundary-adjacent ⇒ cage-match by law.

## Defer, with a trigger — not abandon

### 3. The signed ack (old Tier 2) and the prober (old Tier 3)

**HALTED.** Carnot: DISSOLVE. Kelvin: RECAST-halt. Both correct that a voluntary proof is
theatre against an operator who is the adversary.

The missing component is known and precedented — **client-side enforcement**, exactly how
CT forced CAs to log (Chrome requires SCTs; nothing else compelled anyone). But that only
means anything against an operator Nick does not control, and today he runs every island.

**Revisit trigger: the first non-Nick island operator.** Same gate #3387 already waits on.

### 4. Design 06's KT log stays the long-run answer

Unblocked only by: key rotation/revocation (#3589, #1972, #1865), the multi-device model
(#17), a real gossip transport (#1578, #3800), island-side key→account binding (#3774),
and the app tab lifting its recorded WAIT. **Plus an honest statement that N=2 has no
split-view protection** — both the external research and Design 06's own risk section say
so, and neither the design nor the marketing should imply otherwise.

## Owed to the app tab

- **T-2:** a wire-contract rule that `client_msg_id` must be high-entropy and
  non-correlatable — no embedded account/device/channel semantics, no cross-channel reuse.
  Today's client uses a ULID, which embeds a millisecond timestamp; that is already
  published inside `origin`, so this is about *future* clients, not this one.

## What I got wrong, recorded because it is the transferable part

1. **I filed a disqualifying flaw as a scope note.** Fold found "an island cannot be made
   to sign" and I carried it openly as a stated limit — which felt like rigor and was
   actually the whole mechanism failing. **Naming a flaw is not pricing it.** Both
   adversaries went straight to it.
2. **I invented Tier 2 to have something to build.** Carnot: *"The design should have
   stopped at 'KT later; fix position binding now.'"* Ship-momentum, in a design doc
   instead of a PR, and neither my enthusiasm nor my own Fold caught it.
3. **The thesis itself was a regression of a better position this repo already held.**
   Design 06 had already written the honest limit. That is last session's crux —
   *recollection impersonating a checked record* — recurring one level up, at the level
   of a thesis rather than a file path, and it is the single most transferable finding here.

## Scope stamp

**DESIGN-ONLY. IMPLEMENTATION UNPROVEN.** No code was written. A green design pass is not
a green code pass; Tier 1 gets a real cage-match when it is built.
