# CRUCIBLE — Automated Moderator Agent (bot-as-moderator)

*Ore movement. The pick, the heat, the falsifier. Written at the consent gate 2026-08-03.*

## The pick

An **automated moderator agent**: a bot that reads every message on an island, classifies
it with an LLM, and files reports / takes scoped action — so the operator gets moderation
*labor* without doing the moderating. The island stays in `moderator` mode (honest: the
operator's systems read plaintext); the bot is *what fills the moderator seat*.

Target: local task #27. Substrate verified real — `concept_caged_decider_sealed_sender`
(the pattern), `moderation_service.py` (retraction forward-event + `message_reports`), and
`require_moderator`/`MODERATOR_USER_IDS` (the auth gate) all exist on `main`.

## Why this thrills me — AND what it changes

**The heat:** it turns "I don't want to moderate" into a *self-running immune system* for the
island. And it does it by **welding three things the repo already built** into a capability
none of them delivers alone: the caged-decider/sealed-sender safety pattern (from the Signal
watcher), the retraction forward-event (#7, shipped PR#104), and island agent-ingress (#19).
The bot is a caged decider whose "sealed sender" is the existing takedown verb. That's the
*oh, of course* — the parts have been lying around waiting to be assembled.

**What it changes (impact, not affect):**
- Removes the human-moderation task **entirely** for routine content — the thing Nick
  explicitly said he doesn't want to do.
- Makes a **solo-operated island actually operable at community scale** — you cannot
  hand-moderate a growing community alone; this is the difference between "toy island" and
  "island a real community can live on."
- **Dissolves the enspyr ghost-seat problem we hit *today*** — "no human moderator account"
  stops mattering when `MODERATOR_USER_IDS = [bot-agent]`; the human becomes the *escalation
  target* for the rare legal must-act, not the seat-holder.

## The falsifier (what would prove this ore is slag)

**If the injection cage can't hold, or auto-takedown's false-positive rate makes it useless,
the whole thing collapses to "a human still reviews everything" — which relocates the labor
instead of removing it, killing the impact claim.** Concretely, this ore is slag if EITHER:

1. A crafted message can steer the Judge into actioning a *different* user's messages (the
   cage leaks) — then the bot is a weapon, not a moderator; or
2. Auto-takedown's false-positive rate is high enough that every flag needs human review to
   avoid wrongful removals — then the bot didn't remove the human-moderation task, it just
   renamed it "reviewing the bot," and Nick is moderating again.

Either one means the labor-removal (the entire impact case) evaporates. The temper must
strike (1) and (2) hardest — they are load-bearing, not details.

## Scores

- **Aliveness: 3** — Nick raised it unprompted mid-session, and it assembles three existing
  repo capabilities into one (not a from-scratch invention). "You'd drop other work" — he
  did, we're here.
- **Impact: 3** — removes a recurring human task he named, and unblocks solo-operated islands
  scaling to real communities. Product = 9.

## Output location

`docs/crucible/automated-moderator-agent/` — issue-backed / cross-cutting ore (spans a new
sidecar process + agent-ingress + moderation machinery, no single owning file). Matches the
`reactive-deploy` unnumbered-slug precedent.

## Temper correction (round 1, 2026-08-03)
Unanimous cross-family REQUEST_CHANGES (Carnot/Tesla/Kelvin) downgraded the honest impact from **3 (removes the task)** to **2 (triage assist)** — FN recall ~0.47 means no current classifier delivers fully-hands-off, so the operator can't fully step away. Aliveness still 3. The 'removes a human task' impact claim was inflated; corrected. See DESIGN.md Temper section T1-T10.
