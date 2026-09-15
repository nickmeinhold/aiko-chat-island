# Expiring justifications — when a guard's stated reason stops being true

**Status:** reference, swept 2026-09-15 against `origin/main` @ `a925bec`.
**Instrument:** `git grep` over `src/ deploy/ alembic/` for world-fact justification
language, then each named condition re-measured against the live islands.
**Sibling:** [claude-tasks#4410](https://github.com/nickmeinhold/claude-tasks/issues/4410),
the *reference* sweep — *"does this `#NNNN` resolve?"* This note is the *premise*
sweep — *"what is this sentence's validity conditional on, and has that thing moved?"*
**Neither instrument finds the other's defects**, which is the argument for both.

---

## The class in one line

> **A guard whose STATED REASON has expired while its CONCLUSION stays true.**

Nothing is broken, so nothing draws attention. The next reader inherits a
justification that has quietly stopped justifying anything, and the failure arrives
later — when someone removes the guard *on the strength of the comment*.

This is not "a stale comment". A stale comment that contradicts the code gets caught
the first time someone reads both. This one **agrees** with the code. It is stale in
the reassuring direction, which is the direction that generates no work.

## Why it is hard to see from the inside

Both instances below were found **by accident, while looking for something else.**
That is a poor detection rate for a class whose failure mode is *"reads as safe to
whoever checks the reason."*

The asymmetry that makes it invisible:

| | a guard that BREAKS | a guard whose REASON expires |
|---|---|---|
| symptom | something fails | nothing fails |
| who notices | the next user | nobody |
| what re-checks it | the failure | nothing |
| how it eventually bites | immediately | when someone deletes the guard, citing the comment |

---

## What the sweep found

### 1. `apns.py` — the end-wake window's safety rested on an empty population

> *"THE WINDOW IS NOT REACHABLE TODAY ... an end wake is routed to VoIP rows only,
> and both live islands hold zero (measured 2026-09-11: 3 rows each, all
> `token_kind='alert'`). It becomes reachable the moment a build registers a VoIP
> token. That is the gate."*

**The gate fired.** Re-measured 2026-09-15 against both live databases:

```
chat.enspyr.co       alert 4 / voip 1
chat.imagineering    alert 3 / voip 1
```

The window is still shut — by `push_service.END_WAKE_VOIP_GATE_OPEN = False`, a coded
interlock (#4265, PR #178) that the comment never mentioned. So the sentence was
dangerous in **both** directions: a reader checking the stated reason concludes the
empty population is the guard and that registering a token opens the window; a reader
who knows the population moved concludes the guard is gone. Neither finds the
interlock.

**Fixed:** PR #180.

### 2. `push_service.plan_deliveries` — arm (B)'s deploy-safety argument

> *"(B) ... It is also the only arm with no silent non-delivery, and it makes the
> deploy trivially safe — **every row on both live islands is `token_kind='alert'` by
> `server_default` today** and the alert ring is proven on real handsets."*

Also false, by the same measurement. And this one is **load-bearing**: it is the
stated reason the deploy was safe.

The conclusion survives, for a reason the comment does not give: **arm (B) never
suppresses, so it cannot silently fail to deliver — regardless of the population.**
The property is the argument; the population was never needed.

**This sat one paragraph above a sentence #4410's sweep DID fix**, in the same
function, and that sweep walked straight past it. Two sweeps, one function, two
defects, neither instrument able to see the other's.

**Fixed:** PR #179, as part of restoring arm (B) after its cage-match.

### 3. `preflight-compose-drift.sh` — a count, and the guard invalidated it itself

> *"Measured on both live islands: the box carries FOUR files under `deploy/`."*

Counted 2026-09-15: **five** non-backup entries per box — and the fifth is
`preflight-compose-drift.sh` itself, added by #4230 *after* that measurement.

**Not load-bearing, and recorded as not.** The enumeration is box-driven and dynamic;
nothing reads the number. It is listed because the mechanism is instructive — *a
guard's own arrival invalidated its own comment's count* — not because it is a defect.
Inflating it would make this note the thing it is about.

---

## The convention this argues for, which is nearly free

**When a comment's justification rests on a fact about the world, DATE IT and NAME THE
INSTRUMENT.**

`apns.py` did exactly that — *"(measured 2026-09-11: 3 rows each, all
`token_kind='alert'`)"* — and that is precisely why it took one SQL query to falsify.
`push_service.py` said *"today"* with no date and no instrument, and needed someone to
already suspect it.

A dated claim naming its instrument is **falsifiable in seconds by the next reader.**
An undated one is only falsifiable by someone who already doubts it.

Corollary, and it is the cheaper half: **prefer a PROPERTY to a POPULATION.** Arm (B)'s
safety never needed the row counts — "it never suppresses, therefore it cannot silently
fail to deliver" is true forever. A justification that cannot expire is better than one
that is dated.

## Why no mechanism is proposed

A checker would have to decide whether a prose claim about the world is still true.
That is a judgement, not a lint. Both real instances were caught by **re-measuring
something cheap** — one query against two boxes, about thirty seconds.

This is the same answer #4410 reached by a different route, and the agreement is worth
stating: for both classes, the detector is a person re-reading, and the affordable
intervention is to make the claim *cheap to re-check* rather than to build something
that re-checks it.

## The cross-repo instance, because it makes the class legible

From the `aiko_chat_app` tab, 2026-09-15, on where to store a per-install identifier:

`kSecAttrAccessibleWhenUnlockedThisDeviceOnly` is device-bound and cannot clone — the
property wanted. It is also **unreadable while the device is locked**, and the entire
reason the identifier exists is to be read when a VoIP push wakes a terminated app on a
locked handset. It would work in every test anyone would run with the phone in their
hand, and fail on the one path it is for. (`AfterFirstUnlockThisDeviceOnly` is the
correct member.)

Same family, reached from the other side: **a mechanism whose failure is invisible to
the only test anyone would run.** Both are one thing — *the check and the thing it
protects have quietly stopped being the same thing* — and a cross-repo example makes
that legible in a way four same-repo ones do not.
