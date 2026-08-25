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

---

# Addendum — 2026-08-25 19:30. C-5 RESOLVED, and a correction to this bundle

The app tab (peer Claude session) answered the load-bearing premise. **Verified independently
from this side before accepting it**, not taken on report.

## C-5 is RESOLVED — reach WORKS, and was proven two days before this crucible ran

```
$ ssh nick-mel 'sudo docker logs aiko-chat-island-1 2>&1 | grep -c push.apple.com'
1
INFO:httpx:HTTP Request: POST https://api.sandbox.push.apple.com/3/device/d309f150…9effe "HTTP/2 200 OK"
```

**A ring reached a locked iPhone on 2026-08-23 13:36 AEST**, app closed, through the whole path:
`POST /v1/devices 201`, a real signed call invite through `create_outbound` and all eight
`push_service` gates, Apple returning 200. Nick, on the handset: *"it rang!"*

`device_tokens` reads 0 because the app tab **deleted the row** at 2026-08-25 00:07 UTC as arm 1
of a deliberate two-arm live test of app PR#156's teardown ordering. The test passed.

## The error in this bundle, named

`CRUCIBLE.md` and `DESIGN.md` (C-5) assert *"Nothing has ever registered"* and *"this design's
primary consumer has never once worked end to end."* **Both are false.**

I ran `select count(*) from device_tokens` → 0 and inferred a historical claim from a snapshot.
**"Zero rows now" and "no row has ever existed" are different claims**, and only the second
supports "never reached". The instrument was right; the inference was not. The cheap falsifier —
the container's own retained log — was available and I did not run it.

Treat every C-5 reference in this bundle as **superseded by this addendum**.

## Does this reopen the island half?

**No.** The ruling rested on two Fold findings, neither of which touches C-5:

1. the null option was not null (increments 1–3 ride the existing message path), and
2. the sender anonymity set is the concurrently-online population, which at N=33 can be one.

If anything C-5 resolving makes the ruling **firmer**: reach is proven, so increments 1–3 deliver
real value immediately on a foundation that demonstrably works.

## Two refinements from the app tab, folded into the app-side inheritance

**C-3 (recovery liveness) — a sharper failure mode than I priced.** They confirm the
digest-intersect shape is right and should not change, but supply population evidence: *32 users,
2 membership rows, most recent message four days old.* A recovery path requiring the counterparty
to open the app mostly does not run on that usage. Their reframe is correct: it is not "recovery
fails" but **"recovery has unbounded latency, decided by the least-active friend"** — soft and
detectable rather than silent. Two consequences:
- state it as a **named residual** in the same discipline as the IP one;
- the re-pair prompt must be **durable and re-raisable**, never a one-shot notification dismissed
  at a bad moment.

**And the check they flagged, which is an ISLAND-side constraint even though the island builds
nothing here:** if `GET /v1/rekeyed?since=` has any server-side **retention window**, a 90-day
absent friend fetches an empty list and re-pairing silently never happens — converting the
latency problem into exactly the silent failure C-3 feared. **Whoever builds the endpoint owes
unbounded (or very long) retention of re-key entries.**

**C-7 (block on the device) — strengthened, with an honest cost.** Their argument is better than
mine: the island's block is **already not a delivery guarantee** — it is one of eight gates, and
gate 6 unions the caller's fanout set with the service's own read precisely *because neither is
trusted alone*. Moving enforcement to the device relocates a partial guarantee rather than
removing a total one. Precedent in this codebase: the wake payload is **already deliberately
opaque** (channel id only, no caller name) so Apple never learns who rings whom, with the cost
stated honestly. The project has accepted "the intermediary learns less, the device does more"
once already, for the same reason.
**Honest cost to carry:** a device-enforced block cannot be enforced on a device that is not
yours, so a blocked party still consumes the recipient's wake budget and causes a wake even
though nothing is shown. An attacker cannot ring you, but can drain your battery.

## New product thread surfaced by the app tab

Nick wants **Dreamfinder to call him in the morning and wake him up.** This collides with the
alert-not-PushKit decision: on iOS 13+ a PushKit VoIP push **must** call `reportNewIncomingCall`
synchronously or the system terminates the app, and repeat offences stop VoIP delivery entirely —
which is why this project chose alert notifications. But an alert notification does **not**
reliably wake a sleeping person through Sleep Focus.

**The `urgent` grade is the natural home for that question**, so increment 2 should be designed
with the wake-me-up case explicitly in view rather than discovering it afterwards. This also
confirms the app tab's recommendation (and mine) to **pull increment 2 to the front**.

## The iOS claim I asked them to check — CONFIRMED

They verified against their own record and Apple's docs: a PushKit push must report synchronously
or the app is killed; repeat offences kill VoIP delivery. So an island-wide broadcast either
raises a spurious CallKit UI on every handset (and terminates the ones that cannot decrypt and
therefore do not report), or rides throttled silent pushes that will not reliably reach a closed
app. **The fog does not come back. The sender-side simplification stands.**
