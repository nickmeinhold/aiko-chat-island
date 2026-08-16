# TEMPER-v2.md — a call as a first-class object (island half)

**Overall verdict: DISSOLVE — CANDIDATE INVALIDATED.**
**Struck:** `dt-1786854185`, 2026-08-16. Families seated: **Maxwell (Claude) + Kelvin
(gemini-3-pro-preview) + Carnot (GPT/codex) + Tesla (Grok)** — full 4-way. Wu/Kimi disabled.

**3 of 4 families voted DISSOLVE.** The synthesis rule is *DISSOLVE is decisive at ≥2
families*. Round 2 of ≤3 is spent, and the budget is moot: this is an honest negative result,
not a failure to paper over. **Do not re-cast. Do not hand to Blade.**

## Per-family verdicts

| Family | Verdict | One-line |
|---|---|---|
| Maxwell (Claude) | **DISSOLVE** | The federation claim is false against the running system, and the ring already self-terminates in 30s — both facts the table was justified by are gone. |
| Kelvin (Gemini) | RECAST | The retreat is correct but lands in an unstable equilibrium; the zombie participant is a real bug being normalised. |
| Carnot (GPT) | **DISSOLVE** | Once the row is forbidden from controlling anything, the computation is mostly waste heat. |
| Tesla (Grok) | **DISSOLVE** | The detector's defining input is the input that prevents the detector. |

## Fatal flaws (deduped, most-severe first)

**D1 — The table cannot detect the one thing only it could detect.** *(Tesla; independently
reached by Maxwell from the other end)*
`FOLD-v2.md` S1 reduced the table to two facts, one of which was **involuntary end** — the
caller's app crashed, so nothing was signed. But an involuntary end *is precisely the crash
that leaves a live LiveKit participant behind*, and that zombie is what prevents the room from
going empty. So `empty_reap` cannot fire on the exact event it exists for; liveness collapses
to `max_duration`, a long mood-ring. **The detector is blinded by its own trigger.**

**D2 — The ring already self-terminates, so there was no server-only fact to begin with.**
*(Maxwell, verified in shipped code)*
`aiko_chat_app` ships `kCallRingDuration = Duration(seconds: 30)`. A caller whose app crashes
rings the callee for **at most 30 seconds**, with no island involvement. Increment 0 makes the
stop *prompt*; the 30s ceiling already makes it *bounded*. Combined with D1, S1's two-fact
justification reduces to a **single soft UI hint**.

**D3 — The federation claim in `DESIGN-v2.md` is false.** *(Maxwell, verified against both live
boxes — and note Kelvin explicitly endorsed the false claim, which is why a live probe beats a
panel)*
v2 asserts *"a cross-island call works with neither island having a `calls` row at all — Bob
joins the channel room; Alice is there."* Two reads refute it:
- `room_for_channel()` = `f"{settings.gateway_id}:{channel_id}"`; live values are
  `GATEWAY_ID=imagineering` and `GATEWAY_ID=enspyr` → **different room names for the same
  channel**.
- `LIVEKIT_URL` is `wss://livekit.imagineering.cc` vs `wss://livekit.enspyr.co` → **different
  SFUs entirely**.

The room is not computed identically by both sides from signed data; it is computed by each
*island*, and they disagree. Cross-island calling is broken today, and **neither round 1 nor
round 2 addresses the actual cause.** This was v2's headline benefit.

**D4 — The remainder does not earn its blast radius.** *(Carnot, Tesla)*
For a value the design itself says must not be trusted (`live` is a label, never a gate; `POST
/calls` is advisory), the island absorbs: a schema, two endpoints, a public authenticated
webhook surface, a per-island-LiveKit-keys prerequisite, an account-deletion cascade, and a
presence-disclosure surface. Carnot: *"a write you may skip and a read you must not trust is
not an API."* Tesla: *"It is a souvenir."*

**D5 — Objecthood was preserved as a word, not as a property.** *(Maxwell, Carnot, Tesla, and
Kelvin from the opposite direction — 4 families)*
After the demotion the call cannot decide anything, survive its own end (deleted), federate, or
separate call N from call N+1 at the SFU. Tesla: the ticket's heat was that *a permanent room
cannot tell this call from the next call from never* — v2 keeps a ULID and then refuses to put
it on the invite, the token, or the SFU. **The reversal of #3170 answered a different want and
kept the ticket number.**

**D5b — v2 forecloses the consent boundary it claimed only to defer.** *(Tesla)*
`video-token` is DM-only *because* an eternal conversation-room cannot express per-call consent
— pairwise blocks are unenforceable at a room-level token with unbounded participants. Per-call
rooms were the path to lifting that gate. v2 does not defer that work; by committing to the
eternal room it **forecloses** it, while describing the loss as "objecthood kept". Group calling
(#2731) gets further away, not nearer.

**D6 — `GET /call` is two objects wearing one schema.** *(Tesla)*
`call_id`/`started_at` come from `live_calls`; `participants` is eternal-room occupancy, ghosts
included. After delete-on-end, `200 {live:false, call_id:null}` means *never* **and** *just
ended* **and** *crashed and reaped* **and** *the advisory POST was skipped*. The three-way
distinction that justified the endpoint is unrepresentable. #3159's JSON was kept and its
semantics evicted.

**D7 — Delete-on-end plus #3171 is a signed dangling pointer.** *(Tesla)*
The app tab has **pre-committed** to *invite wire v2 — carry `call_id`* (#3171). v2 mints a
server ULID, hands it out, and then deletes the only referent. Federated signed history would
hold identifiers whose target was demolished on purpose.

**D8 — "Increment 0 is zero island change" is false *as written*.** *(Kelvin)*
If a `live_calls` row exists, the island must observe the new signed sentinel to delete it.
Kelvin is right about the design as cast. Note the direction this cuts: **under DISSOLVE the
claim becomes true**, because there is no row to delete. It is an argument against the table,
not for it.

## Kelvin's dissent, recorded rather than averaged away

Kelvin voted **RECAST**, not DISSOLVE, and made two arguments the majority should not bury:

1. **The zombie participant is a real user-facing bug being normalised.** *"A design that
   normalizes known bugs is unsound at absolute zero."* `FOLD-v2.md` S3 declined to argue it
   away, and Kelvin says declining is not enough. **This survives the DISSOLVE**: the ghost
   exists in the shipped implementation today, independent of whether the island builds a
   table. It should be filed on its own merits.
2. **`live: boolean` is an attractive nuisance.** A safety model resting on future developers
   honouring a design-doc sentence is a future bug report. If occupancy is ever re-cast, it
   should not return a binary that begs to be wired to a gate.

Kelvin also asserted that keeping `room_for_channel` *"correctly dissolves the federation
flaw — there is no second room."* **That is refuted by D3** against the live boxes. A panel
handed a premise corroborates the premise; one live probe refuted three families' worth of
agreement (`feedback_verify_prod_before_multi_round_review`).

## What holds (survived all four strikes)

- **The demotion diagnosis was correct even though the conclusion was wrong.** Eleven of round
  1's fourteen flaws did trace to one decision — a local best-effort row on the camera path.
  Naming that is what made this DISSOLVE visible.
- **`POST /v1/channels/{id}/video-token` must not move.** Nine rounds of cage-matched trust
  boundary; a moved gate is a rewritten gate. v2's reversal of round 1's F14 was right.
- **Signed messages are the correct durable substrate** for invite, end, and missed-call
  history — they federate and carry attribution; a local SQLite CDR does neither.
- **The client must never construct or parse a room name**; it is opaque, from the token
  response only.
- **`live` as a label and never a gate**, count-only participants, existence-hiding 404,
  `no-store`, DM-only on the mutator — all correct constraints for anything that ships later.
- **Increment 0 is the whole product fix**, and under DISSOLVE it costs the island nothing.

## Disposition — candidate invalidated

1. **Build nothing on the island for #3170.** Close it against this verdict rather than leaving
   it open as implied future work.
2. **Increment 0 goes to the app tab**: a second signed sentinel over the door that already
   exists — no island schema, no endpoint, no webhook. This is the fix for the reported bug.
3. **File cross-island calling as its own design (D3).** The blocker is per-island SFUs and
   gateway-namespaced rooms, not the absence of a call object. It was implied-solved by two
   rounds of design and is in fact untouched.
4. **File the zombie participant separately (Kelvin's dissent).** It is real today, in shipped
   code, regardless of this verdict.
5. **If occupancy is still wanted, re-file it honestly** as an advisory presence hint priced
   against its own privacy and webhook cost — not as "a call as a first-class object", and not
   returning a bare `live` boolean.

**Two rounds, four families, zero code shipped — and the honest output is that the island half
of this feature should not be built.** That is the crucible working, not failing.
