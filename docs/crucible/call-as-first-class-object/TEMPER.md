# TEMPER.md — a call as a first-class object

**Overall verdict: RECAST** (round 1 of ≤3)
**Struck:** `dt-1786838000`, 2026-08-16. Families seated: **Maxwell (Claude) + Kelvin
(gemini-3-pro-preview) + Carnot (GPT/codex) + Tesla (Grok)** — full 4-way. Wu/Kimi disabled.
**0 DISSOLVE votes** — the candidate survives; the *design as cast* does not.

> Tesla's first run returned a 104-byte stub reporting a truncated prompt. That is an
> **instrument failure, not a verdict**, and it was re-fired with a compacted bundle rather
> than counted as a dark seat. The second run produced a full strike.

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Maxwell (Claude) | RECAST | Well-built inside a frame it never justified — the signed `call-ended` message dissolves the headline bug and was never priced. |
| Kelvin (Gemini) | RECAST | The time-based binding permits call hijacking, and a shared-secret webhook turns a read credential into a cross-tenant write. |
| Carnot (GPT) | RECAST | "Channel+time is identity" is slag; bind the row to the invite cryptographically and the rest becomes defensible. |
| Tesla (Grok) | RECAST | You minted a telco switch in one island's SQLite and called it an object — it cannot die without locking the channel, or travel without asking a ghost. |

## Fatal flaws (deduped, most-severe first)

**F1 — The synthesis fails: channel+time is not an authorization proof.** *(Kelvin, Carnot,
Maxwell — 3 families, convergent)*
The attack, stated cleanly by Carnot: a hostile island or ordinary delivery delay **withholds
invite A, lets call B start later in the same channel, then delivers A** — the callee resolves A
to B and *a camera is lit from the wrong signed stimulus*. `started_at >= signedAtMs - skew`
only rejects **older** calls; it never binds an invite to a call.
→ **DISPOSITION: fold.** Take Carnot's constructive path rather than Kelvin's v2: bind the call
row to `invite_msg_id` + the **#3167 composite** (signer ‖ channel ‖ clientMsgId), and have the
callee's answer request **present the signed invite identity**, so the island resolves only the
row bound to *that* invite. This keeps `kCallInviteBody` shut while making the binding
cryptographic. If that cannot be made race-free, the call id goes in a **new signed field** (not
an edit to the sentinel).

**F2 — Federation: the call row does not federate, and fail-open puts the two parties in
different rooms.** *(Tesla — 1 family, but the most severe and immediately real)*
Channel messages federate; a `calls` row is local SQLite. **Two islands are live now.** Bob's
home has no row → `GET /call` 404 → the design's own **fail-open** contract rings him anyway →
he joins `room_for_channel` while Alice sits in `call:<ulid>`. **Both honest, both alone.** The
v2 cost was not avoided; it was displaced into a dual-room migration that production runs the
week the first island ships. *"A call that spans two islands is not an edge case. It is the
product."*
→ **DISPOSITION: fold — scope it in writing.** This increment is **same-island DMs only**, and
**fail-open must NOT fall back to `room_for_channel` once a caller has minted a `call:` room**
(mixed-version ⇒ no media, never two rooms). Cross-island calling is a *different design*
(home resolution, webhook fanout, what a dead island leaves in signed history).

**F3 — The zombie live row locks the channel permanently.** *(Tesla)*
Webhook dropped + nobody answers (so the on-answer reconcile never runs) + TTL mis-set or sweep
asleep ⇒ a live row with no call. `POST /calls` is **idempotent-by-liveness**, so it returns the
corpse with `joined: true`, and LiveKit auto-creates on join — *"a permanent join-the-empty-grave."*
**No new call can be minted on that channel until a human deletes the row.** My glare fix
becomes a deadlock.
→ **DISPOSITION: fold.** The unique index needs a **break-glass**: an explicit owner hangup, or a
sweep that *confirms* empty. A liveness bit with no forced-release path is a mutex you must
remember forever — `concept_remove_coupling_not_guard_window`.

**F4 — One TTL cannot be both the stuck-ring scythe and the long-call ceiling; no heartbeat
exists.** *(Tesla, Carnot, Maxwell)*
The four events are `room_started` / `participant_joined` / `participant_left` /
`room_finished` — **none is periodic.** On a healthy 2-hour call the last webhook is the last
*join*, so TTL-from-last-webhook **hangs up working calls** (and frees the mutex, reintroducing
glare as call 2). TTL-from-empty **never fires** when `participant_left` is the dropped packet —
the exact bug, wearing a `GET /call`. And **build step 1 ships this contradiction with no
webhooks at all**: liveness = "younger than the TTL" ⇒ a hard cap on call duration equal to the
ring-stop timer.
→ **DISPOSITION: fold.** Split the number: `empty_reap` (short, only when occupancy is *known*
zero) ≠ `max_duration` (long safety cap). Step 1 uses neither as a stand-in for the other.

**F5 — Per-island LiveKit keys are a PREREQUISITE, not a follow-up.** *(Kelvin, Carnot, Tesla —
3 families, convergent)*
With one shared HS256 secret, a "verified" webhook proves only *"sent by someone holding the
global media secret"* — not *"sent by the SFU, for this island."* If a webhook can write
`ended_at`, **"end anyone's call" is a real remote primitive** among sibling islands. Tesla adds
the second-order cost: `ListParticipants` is **admin API**, so activating it on a shared project
hands every island `ListRooms` over the whole federation — a leaked island env stops being
"forges media tokens" and becomes a live directory of every call on the wire.
→ **DISPOSITION: fold — hard reorder.** #2732 moves **before** the webhook receiver in the build
order. Until then webhooks may update advisory counts only, never terminal `ended_at`.

**F6 — Webhooks must be addressed `room → call_id`, never "the live row for this channel."**
*(Tesla)*
A late `room_finished` after a hangup-and-recall would kill call 2 on behalf of call 1. Combined
with F3/F5 this is a **forced call-rotation primitive** — and the client is specified to trust
the slot.
→ **DISPOSITION: fold.** Invertible room→call addressing, mandatory.

**F7 — `signedAtMs` is attacker-controlled input inside a security check.** *(Maxwell)*
The signature proves the *signer chose* the timestamp, not that it is true. A future-dated invite
satisfies the freshness comparison against any later call, indefinitely.
→ **DISPOSITION: fold.** Use server-observed receipt time (`messages.created_at`). Largely
subsumed by F1, but the lesson stands independently: never gate on client-chosen time.

**F8 — The signed `call-ended` message was never priced, and it dissolves the headline bug.**
*(Maxwell)*
For the *reported* failure — caller hangs up, callee still rings — the caller's own client knows.
A second signed message over the existing door fixes it with **no table, no webhook, no TTL, no
outbound SFU call**. The app tab's *"the app cannot ask LiveKit anything"* is true but irrelevant.
→ **DISPOSITION: fold as increment 0**, then re-scope the table to what messages genuinely
cannot do (glare arbitration, history, involuntary disconnect).

**F9 — "Zero wire-format change" is false.** *(Carnot, Tesla)*
Versioning was displaced from the invite body into **endpoint choreography** the app must
feature-detect anyway. Call it capability negotiation, honestly.
→ **DISPOSITION: named tradeoff** — owner: island tab; cost: the app feature-detects the calls
endpoints; mitigation: state it plainly in the contract and drop the "zero change" claim.

**F10 — Backend-first violation: the DM-only gate is not on `POST /calls`.** *(Maxwell)*
The route is channel-generic, so the island would ship a group-ring primitive the app refuses to
use. → **DISPOSITION: fold.** Gate the mutator.

**F11 — The glare loser path is underspecified.** *(Carnot, Maxwell)*
Insert → catch unique violation → **re-read in a fresh transaction** → re-check `ended_at IS
NULL` immediately before returning, else the loser returns a call the winner already ended.
→ **DISPOSITION: fold** as explicit algorithm, not prose.

**F12 — We accidentally incorporated a phone company.** *(Tesla)*
`started_by` / `started_at` / `ended_at` / `invite_msg_id` / counts = an **unsigned, un-cascaded
(FK off), un-retained CDR** on independently-operated boxes. A malicious operator needs no new
crime — they export who called whom, for how long. Also: two authors for one fact (the glare
loser still *announced* "I started a call" in signed history, while the row credits the winner),
and missed-call rendering will believe the row.
→ **DISPOSITION: fold.** Retention, account-deletion cascade, `started_by` marked
island-asserted-not-federated-signed, and `GET /call` explicitly never a ring trigger.

**F13 — `POST /calls` returns `room`, contradicting this design's own identifier table.**
*(Tesla)*
The table — written *because* of the `dm:` prefix incident — says the client meets `room` only on
the token response. Two sources for the room name, plus v1 `room_for_channel` still minting, is
several ways to light a camera into the wrong cavity.
→ **DISPOSITION: fold.** `POST /calls` returns `call_id` only.

**F14 — The moved `video-token` gate is a rewritten gate.** *(Carnot, Maxwell)*
Existence-hiding must fire **before** revealing that a `call_id` exists; and `is_posting_member`
("may post text") is the wrong verb for "may publish camera into this call" once calls are
first-class consent objects.
→ **DISPOSITION: fold** as an enumerated gate map (`feedback_enumerate_invariant_lattice_before_review`).

## What holds (survived all four strikes)

- **Server-minted ULID + `UNIQUE (channel_id) WHERE ended_at IS NULL` is the right arbitration
  shape.** Every family upheld the `FOLD.md` kill of the derived-room idea: *purity cannot
  arbitrate.* Tesla: *"that metal is good."*
- **The client must never construct or parse a room name.** Existence-hiding 404, 503-when-
  unconfigured, count-only `participants`, `Cache-Control: no-store`, DM-only until #2731.
- **Do not reopen `kCallInviteBody`**, and never put camera-lighting in `kind` — `signingBytes`
  still does not cover it. Forging a *start* must remain a signed message.
- **Webhooks as non-authority is the right direction** — they just must not write the mutex bit.
- **Per-call rooms genuinely close the resurrection/history class** that eternal-room occupancy
  cannot represent; explicit `ended_at` is the irreversibility marker LiveKit's lifecycle lacks.
- **Kelvin upheld the rejection of #3159** — *"the object model is cheaper at the trust boundary
  … the single strongest argument in the entire forge."* **But Carnot and Tesla partly reversed
  it**: the outbound `ListParticipants` returns anyway in the reconcile, so the honest delta is
  *no 1/s polling + history + glare*, not *no outbound dependency*. **Surfaced as a live
  disagreement, not tie-broken.**

## Disposition

**RECAST** — round 1 of ≤3. Fold F1–F8 and F10–F14 into `DESIGN.md`, record F9 as a named
tradeoff, and re-strike. Two changes are structural rather than local and must land before any
build:

1. **#2732 (per-island LiveKit keys) is promoted to a hard prerequisite** — 3 families.
2. **The increment is scoped to same-island DMs in writing**, with fail-open forbidden from
   falling back to `room_for_channel` once a `call:` room exists.

**Not handed to Blade.** The design is not yet plan-ready.
