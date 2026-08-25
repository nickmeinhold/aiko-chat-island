# 🜂 OUTCOME — island half CLOSED, work re-homed to the app

> **Status: UN-TEMPERED.** No `/design-temper` strike was run. Nothing here has survived a
> cross-family adversary. Do **not** cite this bundle as battle-tested, and do not build
> increment 4 off it without a real strike first.

## The ruling

**Nick, 2026-08-25 19:24 AEST**, after reading the Fold pass: *"yeah sounds like an app thing."*

**The island builds nothing for this candidate.** Increment 4 (blind-signed tokens, the
unauthenticated `POST /v1/ring`, sender-anonymity) — the only genuinely novel island-side content
in the design — is **not being built**. Increments 1–3 are app work and need no island change.

## Why the forge stopped here rather than at Temper

Temper is the expensive gate (four families striking the design). Running it on a candidate whose
island half has been ruled out would spend the gate on a moot question. The honest stopping point
is the ruling.

**This is a result, not a failure.** The forge's output is a design that arrived at *"the island
should do almost nothing here, and here is precisely which nothing"* — plus the one thing the
island must never do: **hold the pair.**

## What the Fold pass established that made the ruling obvious

1. **The null option was not null.** Increments 1–3 route rings over the *existing* message path
   (`create_outbound` → `push_service`), which already reaches a handset. So "app-local contact
   list with grades, no island change" — the alternative Cast rejected — **works**, and is what
   increments 1–3 actually are.
2. **Increment 4's property is weak at our scale.** The anonymity set is the concurrently-online
   population; at N=33 that can be one. A ring at 03:00 with a single account online identifies
   the sender with nothing broken. **No fix exists at this scale** — it is a property of the
   population, not the protocol.
3. So the island-side build bought a weak property at a large cost, and everything the product
   actually wanted sat in the increments that need no island at all.

## What carries forward — and where it homes now

Per Nick's 2026-08-23 ADR-homing ruling (island-only here, app-only in the app repo, app+island in
`geekscape/aiko_chat` via PR): with the island half closed, this subject becomes **app-only** and
homes to **`aiko_chat_app`'s ADR series**, not `geekscape/aiko_chat`. That is a change from what
`DESIGN.md` §0 states, and it is a consequence of the ruling.

**The app-side design survives intact and is worth shipping:**

| # | increment | value |
|---|---|---|
| 1 | the tie + consent ceremony (deterministic symmetric `K_ab`, so simultaneous offers converge — Fold F-2) | consented bilateral ties exist; nobody is added without being asked |
| 2 | **graded reachability** — `urgent`/`ring`/`glow`/`silent`, set by the RECIPIENT, enforced on their own hardware | *"emergency bell rings through sleep, kitchen bell only glows"* |
| 3 | invite-accept built on the tie | closes the *nobody is ever asked* gap (`invite_only` = admin-add-only, `models.py:50`) |

**Constraints the app half inherits and must not lose:**

- **Bootstrap is narrow (Fold F-1).** A tie may be offered only to someone you share a channel
  with, or out-of-band via QR/link. ADR-0004 forbids a directory — so a "friend search" affordance,
  the obvious thing an app designer reaches for, is **prohibited**.
- **The island must never hold the pair.** That is the whole C5 argument (island Design 05): the
  friend-grapevine is a push-independent alert only while an island-compromising attacker cannot
  learn who would warn you.
- **Multi-device is unsolved and is also the best recovery story (Fold F-3).** A second device
  needs nobody else's liveness, unlike social re-pairing.
- Grades are **per-tie, per-direction, recipient-owned**. Enforced on the handset, where they
  cannot be coerced or leaked.

## Island-side residue (small, and separately tracked)

- **#3426** — `island_mode` conflates message confidentiality with media confidentiality; LiveKit
  media E2EE is available today. Filed, undecided, **not** part of this candidate.
- **#3253** — `device_tokens` = 0 on both islands. Still the load-bearing unproven premise for
  ANY reach feature, app or island. Claim C-5.
- **#3386** — `APNS_USE_SANDBOX=true` on both boxes; only debug builds could ever ring.
- **#3259** — `push_service` gate 4 requires exactly one peer, colliding with the
  calls-are-gatherings ruling.

## Rulings captured this session (both Nick's, both durable)

1. **Sender-anonymity IS in scope** for the concept (19:23) — superseded in *practice* by this
   ruling for the island, but it remains the recorded intent if the population ever makes it
   worth buying. Memory: `project_friends_sender_anonymity_ruling`.
2. **The island half is an app thing** (19:24) — this document.

## Bar to reopen the island half

Following the precedent set by the call-as-first-class-object invalidation, which set an explicit
reopen bar rather than a vague no:

> **Reopen when the concurrently-online population is large enough that the sender anonymity set
> is meaningfully greater than one, AND a server-side decision is named that must be
> authoritative.** Absent both, increments 1–3 already deliver the product value with zero island
> code.
