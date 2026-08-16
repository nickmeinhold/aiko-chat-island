# Fold — the author's own pre-adversary strike

*Movement 4. No round budget (it's just me). Fold works the metal; it does not re-grade
the ore. Everything found here was folded back into `DESIGN.md` before Temper.*

## The one that killed my favourite idea

The Ore movement's spark was **derive the room name from the signed invite** rather than
mint it — buying call identity while the invite body kept carrying zero parameters and
`kCallInviteBody` stayed shut. I liked it a lot. It is the reason I was excited.

Then I enumerated the degenerate states, and **n=2-simultaneous killed it.**

**Glare.** Alice and Bob both tap Call within the same second. Each signs an invite. A
derived name is a *pure function of the invite*, so Alice derives room `H(alice…)` and Bob
derives `H(bob…)`. Both phones ring. Both people answer — **into their own room.** Neither
connects, and each sees a call that "started" and is empty. That is worse than today,
where the eternal channel room at least guaranteed they met.

The general form is worth keeping: **a derived identifier structurally cannot dedupe,
because dedupe requires a writer that can see both candidates and pick one.** Purity is
exactly what makes derivation attractive and exactly what makes it unable to arbitrate.
Since "a call is one object two people share" *is* the thing we are trying to build,
derivation cannot express the goal.

Server-minting then stops being bureaucratic overhead and earns its keep: it puts a
single writer in the path, which is what makes `UNIQUE (channel_id) WHERE ended_at IS
NULL` enforceable at all.

**But the good half of the reframe survived** and became the design's centrepiece: the
callee still doesn't need the call id in the signed body, because it can **ask the
island** which call is live. So #3170's stated cost — a v2 wire format on a one-way door —
is avoidable even though its mechanism (server-minted rooms) is correct. That is the
synthesis, and I only found it by taking the reframe seriously enough to kill it properly.

## Other degenerate states swept, and where each landed

| State | Finding | Landed |
|---|---|---|
| **n=0** — answer with no call row | Must not 500 or auto-create. `{live: false}`, app renders "Call ended". | GET contract |
| **n=2 simultaneous POST** | Glare. → partial unique index; loser reads the winner's row and returns `joined: true`. | Shape + claim-to-falsify #2 |
| **Invite replayed** (reconnect drain, scrollback, hostile island) | Deterministic derivation made replay a re-entry primitive. Minting + `started_at >= signedAtMs - skew` bounds it. | Claim-to-falsify #1 |
| **Invite retracted / taken down** | Under derivation, unclear what authorizes the token. Under minting the call row is independent of the message — cleaner, but note a taken-down invite no longer suppresses an in-progress call. | Open variable |
| **Webhook dropped** (`participant_left` abandoned) | Call marked live forever → **the ring never stops**, i.e. the exact bug we are fixing, wearing a confident API. → TTL + one reconcile at the on-answer decision point. | Liveness section |
| **Late joiner after room reaped** | LiveKit auto-creates on join, silently resurrecting a "finished" call. Explicit `ended_at` is what makes that inexpressible. | Data shape |
| **One user, two devices** | Both derive/receive the same room; LiveKit identity is `user.id` → known collision (#2730). Not made worse, not fixed. | Scope boundary |
| **Group call** (no single inviter) | Minting has no problem (any member may POST). Derivation had no invite to derive from. Another point to minting. | Shape |
| **Island with video disabled** | Must be **503**, byte-identical to `video-token`, so the app keeps one code path. | GET contract |
| **Non-member probing a channel id** | **404 not 403**, existence-hiding parity. | GET contract |

## Simplest rejected alternative, tried honestly against my own problem

*"Ship #3159 as cast and stop."* It genuinely does fix the reported bug (caller hangs up,
ring stops) and it is a day's work.

I rejected it, and the reason is not "the object model is nicer": **#3159 forces the
island's first-ever outbound call to the SFU, on a path polled ~1/s during a ring.** The
object model answers the same question from island state. So the "heavier" design is
*lighter at the trust boundary* — which is the opposite of how the fork looked from the
issue titles, and is the single most surprising thing in this forge.

## What I could NOT resolve alone (explicitly handed to Temper)

- Whether channel+time binding is sufficient to identify "the call I was rung for", or
  whether the call id must be in the signed body after all. **I have talked myself into
  "sufficient" twice and I do not trust that.** It is claim-to-falsify #1 for a reason.
- Whether the SQLite partial-unique-index glare fix actually holds under two concurrent
  writers, or whether both transactions can observe no-live-call and race. This needs
  someone who will actually reason about SQLite's isolation, not about my intent.
