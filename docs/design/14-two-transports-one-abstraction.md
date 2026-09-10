# Design 14 — Two transports, one abstraction: what six review rounds were actually about

**Status:** DRAFT, written for `/design-temper`. Not a decision.
**Origin:** PR #172 (`feat/ring-transports-2`) ran six cage-match rounds. Real findings per
round: **5, 5, 1, 2, 4, 3** — not converging, and rounds 4, 5 and 6 each found a defect
*introduced or missed by the previous round's fix*. That is a design signal, not a code one:
a diff reviewer cannot see the shape that keeps generating instances.

This note states the two questions underneath, and is deliberately **not** a proposal. The
instances are already fixed and merged into the branch; what follows is the thing that keeps
producing them.

---

## The pattern, stated before the questions

Every round produced a finding of the same family, at a different address:

| round | instance |
|---|---|
| 1 | `is_configured()` counted 4 of 5 credentials; `delivered_to=0` alarmed forever on a dead-but-unreapable token |
| 1 | fixtures enumerated 4 of 5 credentials |
| 2 | `_UNREACHABLE_REMEDY` told operators to build the half-set that boot-refuses |
| 2 | `FakeApns` mirrored `apns._reap_order` instead of calling it |
| 4 | PR body claimed a capability the code refuses |
| 4 | `_UNREACHABLE_REMEDY[FCM]` told operators to set what the guard rejects |
| 5 | the warning **template** did the same thing the remedy had stopped doing |
| 5 | `FakeFcm` still mirrored, one transport over |
| 5 | `ReapOrder()`'s default was the *destructive* value |
| 6 | `APNS_TOPIC` / `APNS_VOIP_TOPIC` had no invariant between them |
| 6 | the bearer-drop rule keyed on HTTP status, catching device-level 403s |
| 6 | the totality sweep would have *argued against* the router's deliberate skip |

Two roots, and they are independent.

---

## Question 1 — Should APNs and FCM share one result abstraction at all?

`SendResult` / `Verdict` / `ReapOrder` were extracted so both transports could feed one reaper.
But the two protocols do not carry the same **evidence**:

| | APNs | FCM |
|---|---|---|
| death signal | `410 Unregistered` | `UNREGISTERED` in an `FcmError` detail |
| **timestamp** | **yes** — `timestamp` in the 410 body | **none** |
| reap authority | bounded: delete only if not re-registered since T | unbounded: delete if the row still matches |
| auth failure | local signing; the boot guard already caught a bad key | a network exchange that can fail independently |

The shared type has to express both, so it grew a field that means *"no date"* — and because a
dataclass field wants a default, the **weaker, destructive** reading became what silence
constructs. `ReapOrder()` was unbounded delete permission built from an omission, on the one
irreversible operation in the module. Round 5 removed the default; the question that survives is
whether one type should be expressing two different strengths of proof at all.

**The open question:** is `ReapOrder | None` the right shape, or should the transports return
*different* types — say `ProvenDead(since: datetime)` vs `ReportedDead()` — so the reaper is
forced to branch on the strength of the evidence rather than on the presence of a field? A single
type with an optional date lets a caller forget the date; two types make forgetting
unrepresentable, which is the move that has worked every other time in this codebase (the boot
guards, the `String(16)` width, the E2EE refusal).

**What is NOT known:** whether the reaper genuinely wants to act differently on the two, or
whether FCM should simply never reap. Nobody has measured what Google's `UNREGISTERED` means
across a token refresh. Tracked: `claude-tasks#4199`.

---

## Question 2 — What does an island do with a transport whose consumer does not exist?

The FCM send path is complete and correct and **cannot be used**, because the Android client has
no receive half: a data-only wake produces no tray entry, and the app's only Android consumer is
a tray-tap handler. With a credential present, FCM answers 200, the island logs
`verdict=delivered`, `delivered_to=0` stays quiet, and the handset does nothing.

Three postures were occupied in one week, which is itself the finding:

1. **Documented** — a HARD GATE in capitals in `fcm.build_message`. *Failed within hours:* the
   credential was provisioned on both islands by someone who had read that gate.
2. **Refused at boot** — where the branch is now. Honest, and it made the state unrepresentable.
   But it put the island's *operator-instruction surface* in permanent contradiction with itself:
   round 4 and round 5 are both instances of a warning telling an operator to do the thing the
   guard rejects, and there is no FCM preflight, so obeying it crash-loops a live box.
3. **Not built at all** — which is what `transport_not_built` used to say, loudly and honestly.

**The open question:** is "ship the transport, refuse the credential" a coherent posture, or is it
a half-state that necessarily generates contradictory operator guidance? The alternative is that a
transport ships *with* its consumer or not at all — which would mean this PR should carry the APNs
half and leave FCM on a branch until the Android receive half exists.

**The cost of the current posture, stated plainly:** every sentence the island says to an operator
about FCM has to be special-cased, and two rounds' worth of findings were exactly that
special-casing being missed. A design that requires prose to be correct in N places has already
lost, because prose is where this codebase's guarantees go to rot.

---

## Question 3 (smaller, but it recurred) — who owns operator-facing instructions?

`_UNREACHABLE_REMEDY`, the log template around it, `docker-compose.yml`'s comments, the boot
refusal's message and the PR description all describe how to configure push. Four of them
contradicted an enforced rule at some point during these six rounds, and the tests pinned each
half **in isolation** so the contradiction was invisible to a green suite.

**The open question:** should operator-facing guidance be *derived* from the enforced rules rather
than written beside them — e.g. the remedy for a platform generated from the same structure the
validator reads — so that a rule change cannot leave its instructions behind? Round 5 added a
collision test that renders the real warning, which catches the current instance; it does not stop
the next surface from drifting.

---

## What a temper should attack

- Is Question 2's posture actually coherent, or should FCM be split back out?
- Does Question 1's shared type buy anything the reaper uses, or is it premature unification?
- Is Question 3 worth a mechanism, or is a rendered-output test per surface sufficient?
- **Is this note itself the wrong frame?** Six rounds of instances may share a cause that is none
  of the above — the author has been inside this diff for a day and is the least likely person to
  see it.
