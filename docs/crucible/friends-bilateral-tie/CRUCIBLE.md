# 🜂 CRUCIBLE — the consented bilateral tie

> Movement 1 (Ore) + consent gate. Candidate **pre-selected** by Nick 2026-08-25 (no re-scout).
> Task [#3420]. Design PR homes to `geekscape/aiko_chat` per the 2026-08-23 ADR-homing ruling.

## The pick

**A consented bilateral tie** — the primitive aiko chat has nowhere today. Every relation the
model currently holds is *unilateral*: memberships, blocks, mutes. Nothing in the system
represents *two people who both agreed*.

Two consumers, and the bet is that they are **one primitive**, not two that rhyme:

1. **Call-reachability — who may ring whom.** The genesis and the primary consumer.
2. **Invite-accept.** `invite_only` is only a `JoinPolicy` value meaning *admin-add-only*
   (`models.py:50`). There is no pending-invite row and no invitee consent step. Nobody is
   asked; they are added.

## Why this one — the heat

Because the island can already wake a sleeping phone, and has no idea who is allowed to.

`push_service` is genuinely good work: eight gates, a single door, and a gate-0 structure that
makes block/ban/idempotency traverse *for free* because a wake can only ride downstream of an
accepted write. It is careful about **how** to ring. It has no concept whatsoever of **whether
this person ever agreed to be reachable by you.** The only proxy is "you two share a 1:1 DM" —
which is not consent, it is co-location.

And here is the part that made me want to build it: **the answer may not be a table at all.**
The call-object strike prescribed *signed messages as the durable substrate*. A friend tie is
two signed consents. If the tie is a pair of signed messages the island can *verify* but not
*enumerate*, then the island gains the ability to authorize a ring without ever gaining a
queryable social graph — which is precisely the box ADR-0004, app Design 05, and island Design
05's C5 all independently draw around it. Three separate constraints that look like they are
fighting the feature may in fact be *specifying* it.

**What it changes, concretely:** Nick's own ask — *"It'd be great if you had an acc on Aiko chat
and could call people who you friended"* — is one primitive away, and that same primitive is what
turns `invite_only` from *you were added* into *you were asked*. Consent is currently absent from
a chat product, which is a strange thing to be able to write down.

## The falsifier — what would prove this ore is slag

**If the tie must be enumerable server-side to do its job, the candidate is slag.** The whole
reason to build it here rather than as an app-local contact list is that the island must *act*
on it (authorize a wake). If it turns out the island cannot authorize without also being able to
answer "who are X's friends?", then we have built the queryable social graph ADR-0004 forbids and
C5 depends on not existing — and the honest move is an app-local contact list plus a
capability-token the island verifies blind, not a friend feature.

Second, cheaper falsifier: **if a capability token alone suffices, the tie is unnecessary.** If
"B may ring A" can be a bearer credential A hands B out-of-band, the island needs no relation at
all — it verifies a token. That is the simplest rejected alternative and Fold must try to kill
the candidate with it.

## Clearing the prior strike's bar (scout-memory rule)

The **call-as-first-class-object** candidate was INVALIDATED 2026-08-16 (3/4 DISSOLVE). Its
reopen bar, set by all three DISSOLVE votes:

> **"Name a server-side decision that must be authoritative."**

**This candidate clears it, and the distinction is the reason it is a different candidate.**
That forge wanted the island to hold a *record* of something the client already knew (call
occupancy, start/end) — a CDR, derivable and therefore not authoritative. This one asks the
island to hold an *authorization*: **may A cause B's phone to ring.** A client cannot be trusted
to answer that about itself; it is a trust boundary by construction, and the island is already
the sole enforcement point (`push_service`). Admission-to-reach is a server-side decision that
must be authoritative, and there is no client-side reconstruction of it.

**Its prescribed shape binds too, and is welcomed rather than worked around:**
- *"signed messages are the right durable substrate for invite/end/history, not a local CDR"* —
  adopted as the leading shape for the tie itself.
- *"`POST /v1/channels/{id}/video-token` must not move"* (nine cage-matched rounds) — respected;
  this candidate adds an admission predicate, it does not relocate the token endpoint.
- *"the client never constructs or parses a room name"* — respected.

## Stated constraints, carried in at round 0 (not to be rediscovered)

**C5 — the hard one.** Island Design 05 (guardian approval quorum; tempered four-lens
2026-07-10, Nick ruled 07-11, BUILT AND SHIPPED as migration 0013, deploy-dark) downgraded
finding C5 from *sole defence against guardian collusion* to *backstop* on this argument:

> "a quorum attack is non-silent (needs k friends actively asked) — the friend-grapevine is a
> push-independent alert."

That holds **only** because the island stores guardians as opaque Ed25519 approver pubkeys, not
identities: *"Approver keys are independent of any aiko account — a guardian need not be an aiko
user"*; *"the social graph is opaque (keys, not identities)."*

**The claim to strike at (mine, not the record's):** an island-legible friend graph erodes C5
**even if the two relations are never unified** — people pick guardians from their friends, so a
friend list is a high-probability *superset* of the guardian set, and an attacker who compromises
the island gets a ranked list of exactly who must not be warned. C5's downgrade was priced
against the old exposure. **This is an inference and must be attacked, not accepted.**

**Three relations — do not collapse them:**

| | stored as | island's role | authorizes | in ADR-0005 graph? |
|---|---|---|---|---|
| `vouches-for` (creator→bot) | Principal→Principal, bonded/conserved/slashable (ADR-0006) | stake-holder | standing | **yes**, one of five edges |
| guardian | opaque pubkey; counterparty need not be a user | verifier only — *"the island is never in the guardian path"* | takeover (re-bind a passkey) | **no** — deliberately outside it |
| friend (would-be) | must resolve to a routable identity | **actor** — it routes | reach | would be new |

**Also binding:** ADR-0004 *"no central directory"*; ADR-0005's invariant that every stake,
slash, bond and rate limit operates on the Principal graph **only**; app Design 05's *"the graph
never crosses to the client."*

## Verified production premises (measured 2026-08-25 — do not re-assume)

- **Reach is DEAD in production.** Both islands `ISLAND_VERSION=0.7.0`. `push_service` complete;
  `APNS_*` configured **and** forwarded into both containers. But
  `select count(*) from device_tokens` = **0 on both islands**. Nothing has ever registered.
  Cause handed to the app tab (not-wired / silently-failing / no-build-has-run). Filed on #3253.
- **`push_service` gate 4 requires EXACTLY ONE PEER**; gates 2+3 require a DB-confirmed private
  DM. The only wake the island can perform is 1:1 — colliding with Nick's 2026-08-17 ruling that
  **calls are gatherings, not channel properties**. Filed on #3259.
- **`APNS_USE_SANDBOX=true` on both boxes** — only debug builds could ever ring (#3386).
- **Greenfield in code**: zero friend/contact/relation/follow/mutual island-side (one unrelated
  grep hit, `users_service.py:198`).
- **Gate 0**: a wake rides only downstream of an accepted `create_outbound`. Any design that
  wakes a device *without* an accepted message behind it breaks that property and **owes its own
  gate map**.

## Open questions that are NICK'S — surface, never decide

1. Is a friend tie **Principal-level** (portable across islands, per ADR-0005's invariant) or
   **per-island**? The big fork; collides with ADR-0004's no-central-directory.
2. Must the tie **survive the island dying**? Design 05's open question 2 asks exactly this of
   the recovery policy and parks it as a federation follow-up.
3. Should **banning an agent** differ from banning a human? ADR-0005 Model B backs every bot with
   a vouch bond from its creator, so banning an agent plausibly implicates its voucher (#3421).

## Scores

| axis | score | evidence (a reason, not a feeling) |
|---|---|---|
| aliveness | **3** | Nick raised it unprompted and twice; two independent gaps (reach, invite) collapse onto one missing primitive; the app tab reached the same conclusion from the other side without being told. |
| impact | **3** | It is the authorization the island's *shipped* wake path has no concept of, and it is the last primitive between here and "an agent with an account can ring someone it friended." |

Product **9**. Bar cleared. Consent given (Nick, 2026-08-25, 09:22 AEST).
