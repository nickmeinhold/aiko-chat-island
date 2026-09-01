# 🜂 Cast (2nd pass) — sign the ack you already send

*Movement 4, second Cast by the same author after Fold. **SCOPE STAMP: DESIGN-ONLY.
IMPLEMENTATION UNPROVEN.** A green design pass is not a green code pass. Cast 1 is
preserved verbatim at `DESIGN-cast1.md`; this supersedes it.*

---

## 1. What this forge actually found

Nick asked for an auditable island, for **anyone**. I carried in the thesis *if every
island can prove it behaves, there is nothing left to be reputable about.*

**That thesis is refuted, twice, from opposite directions — and the second one matters
more than the first.**

- **Externally** (`RESEARCH-priorart.md`): every *deployed* transparency system kept an
  admission layer for its proof-sources. Chrome admits CT logs at its own discretion,
  with uptime SLAs and an operator-diversity rule; Signal's 2026 KT routes client verification through two
  named third-party auditors, Cloudflare and Trail of Bits. CONIKS promised no auditor class and every production
  descendant hired one, because self-monitoring cannot supply cross-client agreement.
  At **N=2 the gossip graph is a single edge**, so split-view detection is theatre here.
- **Internally** (`RESEARCH-crosstab.md`): `docs/design/06-identity-and-trust.md`
  **already holds this thesis**, sourced, and already states the limit my slogan
  regressed — *"'No central point' is not a shipped outcome … The achievable, honest
  claim is 'a per-island auditable directory and no global trusted root'."*

The second one is the finding. **The slogan Nick and I landed on last session was a
regression of a more careful position this repo already held.** Not a new idea that
needed research — an old, better idea we talked past. That is the previous session's own
crux (*do not let recollection impersonate a checked record*) recurring at the level of a
thesis rather than a file path, and it is the single most transferable thing in this
bundle.

### The incumbent answer is right and currently unbuildable

Design 06 Decision 3's KT log is not wrong. It is **gated**, on things absent in both
repos: key rotation/revocation (#3589, #1972, #1865 — *a KT log with no revocation
semantics logs an unrevocable key*), the multi-device model (#17), a gossip transport
that is unbuilt and untrusted (#1578; #3800 — `peers_service` is *"TEST-GRADE, POISONING
UNDEFENDED"*), island-side key→account binding (#3774 — `record_signing_key` *records
rather than enforces*), and an app tab that has recorded **WAIT** on drawing any ✓ from
it (`key-continuity/DESIGN.md`).

So the design question became: **what is checkable by anyone, needs no second observer,
leaks nothing, and is worth building even if the whole auditability story is
abandoned?**

---

## 2. The shape, in four tiers of decreasing certainty

Deliberately ordered so the confident part ships first and the speculative part is
severable. **Tiers 0 and 1 stand on their own merits. Tiers 2 and 3 are the actual
crucible bet and Temper should strike them hardest.**

### Tier 0 — show Andy the answer to his own question *(not code)*

claude-tasks **#2161**'s remaining steps are *"(3) show Nick BEFORE posting; (4) post to
aiko_chat Discussions."* Andy Gelme raised the "softened CA" question that started this
entire thread on **2026-07-17**. The answer is written, sourced by two adversarial
research passes, and sitting in Design 06 where he cannot see it. **Six weeks, one
review pass, zero engineering** — and it is the only step here that involves the person
whose question this was. Depends on nothing below.

### Tier 1 — echo the row's `client_msg_id` in `message_view` *(a real defect, one field)*

Fold measured the exact shape of a hole the app tab had described more loosely:

| Layer | State | Evidence |
|---|---|---|
| Write | binding **IS** enforced, fail-closed | `signing.py:297` `if cmid != frame_client_msg_id: raise OriginError` |
| Storage | column exists, **UNIQUE per channel** | `models.py:545`; `models.py:531` `UniqueConstraint("channel_id","client_msg_id")` |
| Read | **row's column never echoed** | `messages_service.message_view()` `:48-70` |

The `origin` envelope carries its *own* `client_msg_id` and that IS echoed — which is
exactly why the app tab called the check **self-referential**: the envelope agrees with
itself, and nothing binds it to the row. A dishonest island can attach a validly-signed
origin to a different row and no reader can detect it.

**The fix is one field in the single serializer** through which REST history, WS fanout
and bus-ingest fanout all pass. It converts an already-enforced write-time invariant
into a reader-checkable one. Omit-when-absent (matching `origin`/`mentions`), because
bus-born rows have no `client_msg_id`.

This is the honest floor of the entire forge. It is worth doing if everything below
dies.

### Tier 2 — sign the ack *(the crucible's actual proposal, and it is one signature)*

Fold's best finding: the receipt already exists.

```python
# realtime/envelopes.py:28  — called at realtime/ws.py:225
def ack(client_msg_id: str, msg_id: str, created_at: str) -> dict:
    return {"type": "ack", "client_msg_id": client_msg_id,
            "msg_id": msg_id, "created_at": created_at}
```

That is a statement, by the island, to exactly one party, about **which position it gave
your message** — already computed, already on the wire, already delivered to the right
recipient. It is merely **unsigned**.

Signing it costs a new domain tag (`aikochat:island:ack:v1:EdDSA`), the existing
`island_identity` Ed25519 key, and the canonical length-prefixed byte discipline this
codebase already practises in two places. **No new frame. No new delivery path. No new
storage. No migration. No `MANIFEST_KEYS` change.**

What it buys: the holder of a signed ack has the island's own signature over a position
claim. Because `UniqueConstraint("channel_id","client_msg_id")` makes a resend no-op to
the *same* row, an honest island's acks are **replayable but never contradictory** — so
two different `msg_id`s signed for one `client_msg_id` is a self-authenticating
contradiction, verifiable offline by anyone, forever, needing no quorum, no auditor, no
log and no second observer.

### Tier 3 — the conformance prober *(the "anyone", made literal — and the weakest tier)*

A small standalone client anyone can run, which behaves like an ordinary user, knows the
protocol's correct responses by construction, collects signed acks, and checks them. It
holds no privilege and needs no admission. It is the piece that makes Nick's *"I want
anyone to be able to do that"* mean something operational rather than aspirational.

**Gated on #2235** (local multi-island test harness, already open): a prober is a probe,
and pointing one at `chat.imagineering.cc` spends real users' island. Its safe target is
named before it runs anywhere real.

---

## 3. The limit that caps this whole design, stated up front

**An island cannot be *made* to sign.** An operator can strip the signature and return
the old unsigned ack, indistinguishable from an island that simply has not upgraded.
Nothing here makes signing load-bearing for the island's own operation — which is
precisely the property Kelvin's spark had and which I discarded along with its public
log, because a public routing log is a published social graph and collides head-on with
a decided ruling.

**Consequence, stated as a limit rather than hidden as an assumption: the signed ack is
evidence when offered, and its absence proves nothing.** This design defends against *an
island that upgraded and then misbehaved*, and **not** against a hostile operator who
never opted in. I could not find a fix that survives the privacy constraint.

Whether that cap makes Tier 3 worth building is the question I most want Temper to
answer.

---

## 4. Blast radius and consent spine

- **`MANIFEST_KEYS` untouched.** Exact-set check at verify time ⇒ an added key is a
  structural reject on every peer, and that `v` bump is spoken for by #3731. The signed
  ack is a **separate object with its own domain tag**, following the existing
  `signing` / `island_identity` domain-separation discipline rather than extending the
  manifest.
- **No schema change, no Alembic head, no `batch_alter_table`.** ISL-0001 and ISL-0002
  are untouched through Tier 2.
- **Sender anonymity and no-refused-ring-record hold by construction, not by guard.**
  Nothing is published; a receipt goes to the party who was already there. The coupling
  is removed rather than mitigated.
- **Trust boundary ⇒ cage-match by law.** Tiers 1–2 touch the wire and a signing
  surface. This is a design; the code gets a real adversarial review.
- **Cross-tab:** Tier 1 changes a field every client reads and Tier 2 adds a wire
  object. Per project CLAUDE.md both need the app tab's agreement **before** merge, and
  the island deploys first (silent-desync rule).

## 5. Claims to falsify

1. **C-1 → now a stated limit, not a claim** (§3). Refuted by Fold; carried openly.
2. **C-2. The prober can tell "wrong" from "unlucky."** Heat's strongest empirical
   finding is that most real transparency failures were **operational** — botched
   restores, a reused test key, a dead operator, a cosmic-ray bit flip. A prober that
   reads a crashed island as a lying one manufactures false accusations, which is worse
   than no prober. **Unresolved.**
3. **C-3. Tier 1 is genuinely unowned.** Absence-of-evidence; coverage boundary is the
   ten global tracker searches in `RESEARCH-crosstab.md` §5.
4. **C-4. A conviction is non-comparative in practice.** Once two islands both have zero
   convictions, choosing between them is a judgement this design claims to have
   dissolved. Heat watched exactly this creep happen in CT.
5. **C-5. Echoing `client_msg_id` on every read path is privacy-neutral.** It is a
   client-chosen opaque value now visible to every channel member. Probably fine;
   **not verified**, and it is a wire-visible change to every message.
6. **C-6. The app tab will take this when it has declined the adjacent thing.** I argue
   the ack binds *position*, not *identity*, so `key-continuity`'s WAIT does not apply.
   That is my reading of another tab's ruling. **They own it, not me.**

## 6. Rejected alternatives

- **Build Design 06 D3's KT log now** — rejected on gates, not merit (§1). Still the
  right long-run answer; this is not a replacement.
- **Kelvin's public receipt log** — a published routing log is a published social graph;
  collides with a decided ruling and re-walks the deadlock that killed CT gossip.
- **Maxwell's expiring-delegation island** — genuinely interesting; a re-architecture of
  what an island *is*, not an increment. Filed as a thought.
- **Age-of-externally-attested-history** — dies exactly as the Ore falsifier predicted:
  it requires comparing islands, a comparison is a ranking, a ranking is the reputation
  system #1569 refuted. **The falsifier fired and the by-product did not survive it.**
- **An admitted third-party auditor** (WhatsApp/Signal shape) — works, field-proven, and
  reintroduces the admission decision Nick's "anyone" was reaching past. Reopen if
  Tier 3 fails.

## 7. Open variables

- **V-1.** Should the signed ack cover a body hash, or position only? Body coverage is a
  stronger artifact and a bigger privacy object.
- **V-2. RESOLVED by Fold** — the island does assign the ULID and it *is* known at ack
  time (`ws.py:225`).
- **V-3.** Does the prober need an identity, or can it be anonymous? Identified is easier
  to discriminate against; anonymous is harder to rate-limit honestly.
- **V-4.** `client_msg_id` is `nullable=True` and SQLite treats NULLs as distinct, so the
  uniqueness property Tier 2 leans on **holds only for client-submitted rows**.
- **V-5.** Tier 1 is island-only; Tier 2 needs app agreement before merge.
