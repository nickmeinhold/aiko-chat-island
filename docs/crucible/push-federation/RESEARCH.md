# Push federation — who is allowed to ring whom

**Status: HEAT ONLY. No Cast, no Fold, no Temper. Nothing here is decided, and
several load-bearing premises are UNVERIFIED — see §6 before building on any of it.**

Captured 2026-08-23 from a design conversation with Nick, the morning after the
APNs send path went live on imagineering (v0.7.0, task #3375). Written down
because it existed only in chat scrollback, and because it is the Heat a future
`/crucible` should start from rather than re-derive.

The question: **how do different island operators get to ring a user's phone?**

---

## 1. Why this is hard, stated once

Apple's APNs auth key (`.p8`) **has no sub-scoping**. It is issued to a Developer
Team, you name the `apns-topic` per request, and that is the entire access model.
There is no concept of "this credential may ring users of island X but not island
Y", and no reason to expect one.

So "each operator holds the key" is not a *degree* of trust. It means literally:
**any operator can ring any user of the app, with any alert text, at any hour.**

### The discriminator is revocation, not secrecy

This is the argument the decision should hang on, and it is not about whether
operators are trustworthy.

- **N operators holding the key:** revoking one means ROTATING the key, which
  invalidates it for everyone simultaneously. Every island must redeploy in a
  coordinated window, and until they do, nobody's phone rings. You cannot eject a
  bad actor without an outage for every good one — and you only ever rotate under
  time pressure, because you only rotate when something has already gone wrong.
- **A relay/tower:** revoking an operator is deleting a row. Nobody else notices.

The asymmetry is permanent and worsens with every operator added.

---

## 2. The distinction that decides everything

Three push systems; three different things the credential is bound to.

| system | credential bound to | who can ring? | one app + many ringers? |
|---|---|---|---|
| **APNs** (iOS, macOS) | the **app** (bundle id) | exactly one holder | **impossible** |
| **Web Push / VAPID** | the **server** | every server, independently | native to the design |
| **UnifiedPush** (Android) | the **user's chosen distributor** | whoever the user permits | native to the design |

**APNs binds the key to the app; the others bind it to the ringer.** That single
line settles most of the argument: peer-provided ringing is not a compromise on
Web Push or UnifiedPush, it is how they already work. Only APNs forbids it.

Consequence for what aiko_chat_app ships today (targets: `android`, `ios`,
`macos` — no web target as of 2026-08-23):

- **Android — peer-ringing is buildable NOW**, with the single app already
  shipped. No new publisher, no new credential, no tower.
- **iOS + macOS — both APNs.** One credential holder, whatever it is called.
  (macOS inherits this; it is not "just an iPhone thing".)

---

## 3. What makes DNS's centralisation tolerable

Nick's framing: Andy is building a registrar-of-registrars, DNS-shaped, a set of
servers around the world — could push do something similar? Worth naming the
actual properties, because they form a checklist to hold designs against:

1. **The root is replicated across mutually-independent operators** — 13
   identities, ~1500 anycast instances, 12 organisations. No single party *is* the root.
2. **The root holds almost nothing.** It holds DELEGATIONS, not answers.
   Authority flows downward; the root never answers for your domain.
3. **It is cacheable**, so it is off the hot path.
4. **It is verifiable** — DNSSEC. You trust a signature chain, not an operator.
5. **The root key is under M-of-N split control** in public ceremonies.

Pattern: *centralise the minimum, replicate the holder, delegate authority
downward, make it verifiable, keep it off the hot path.*

---

## 4. The option space

### 4.0 Dead end, recorded so it is not re-proposed: threshold-signing the APNs JWT

The obvious DNS-root-KSK analogue: split the `.p8` with threshold ECDSA so no
single tower operator can sign alone.

**It does not work.** The APNs JWT is not per-message — it is a **bearer token
good for an hour against every device and every topic in the team**, deliberately
cached and reused (Apple rejects providers that mint too often; see
`_TOKEN_REFRESH_SECONDS` in `domain/apns.py`). A threshold ceremony would
authorise *"the group minted a token"*, not *"the group approved this ring"*.
Whoever holds the minted token owns everything for an hour.

Buys: no unilateral minting, auditable minting. Does not buy: per-ring control.
Enormous complexity for that. **Rejected.**

### 4.1 Every island holds the `.p8`

Current shape if it spreads. Any operator rings any user. Revocation is the
killer (§1). **This is the option to avoid by default**, which costs nothing today
and is expensive to undo later.

### 4.2 One shared relay — the "bell tower" of app-repo design 07

`aiko_chat_app/docs/design/07-notifications-federation-ready-explainer.html`
(last touched `ca84773`, 2026-07-24) records this as the target:

> The push relay is the single component that holds the platform keys — APNs /
> FCM, welded to one app. It doesn't hold your account, your channels, or your
> history; those stay federated across islands. It is the smallest possible
> centre the phone platforms force.

> Today there's one island and it rings its own bell — simple. But the shape is
> drawn for a coast full of islands from the start.

Note this explicitly blesses the present state. The gap is that the ISLAND repo's
record has zero mentions of a relay — the seam is real but unowned.

Design 07 also specifies the envelope carries **only an event reference and a
count — the `event_id_only` default**. NOT YET VERIFIED against the island's
actual payload; see §6.

### 4.3 Many towers, partitioned by app publisher — "Apple's constraint as the partition key"

Instead of one tower, **make "app publisher" a federated role with N of them.**
Users pick a tower by picking a client, the way they pick an email app. An island
serves users across several towers, holding a relay-client for each.

**Apple enforces the partition for free**: a tower physically cannot ring users of
another tower's app, because the credential is welded to a bundle id. The cage
becomes the isolation boundary. A malicious tower's blast radius is exactly the
users who chose it.

Prior art at planetary scale: **email.** Gmail, Outlook, Fastmail and Thunderbird
each hold their own APNs credential and ring their own users; any IMAP server
federates with all of them. Nobody calls email centralised because Apple Mail has
a push key.

Maps directly onto Andy's structure: **the tower set is a delegation table**,
exactly like NS records. Which tower serves a device is a delegation the device
declares at registration; the known-tower set can live wherever Andy's registrars
live. This is the literal "something similar to DNS" — same data structure, same job.

### 4.4 Ringing as a service islands provide to each other (Nick's pick)

Drop the dedicated tower. If island B needs to wake a device it cannot reach but
island A holds a credential that can, **B asks A**. "Tower" stops being a box and
becomes a ROLE, discovered through the same registrar structure as everything else.

- No new infrastructure, no new legal entity, no new uptime commitment.
- Degrades gracefully: if no peer can ring you, you are simply not woken — the
  pre-push status quo, not an outage.
- Makes "can ring" a federated capability with a discovery mechanism, which is
  the same shape as the rest of the system.

**Cost, stated plainly:** island A learns that island B wants to wake user U at
time T. With `event_id_only` that is metadata, not content — but it is real
metadata crossing a trust boundary, and a dedicated tower does not leak it
between operators the same way. A genuine trade, not a rounding error.

**Does this force multiple apps? NO — except on Apple.** With one app on APNs,
peer-ringing collapses to one of three things:

1. *One island holds the key, others ask it.* A tower wearing an island's
   clothes — peer-to-peer in vocabulary only. Fine, but do not let the framing hide it.
2. *Several islands hold copies.* Real peer-ringing; reintroduces §1 entirely.
3. *The key lives in an attested enclave several islands can run.* Islands
   OPERATE ringing without HOLDING the key; no operator can extract it,
   attestation proves the code. The genuine analogue of the DNS root KSK in HSMs
   under ceremony. Real (Nitro Enclaves, SEV-SNP) and serious infrastructure.

Only (3) is honest peer-ringing on iOS with one app.

### 4.5 The key insight: peer-ringing is the right ABSTRACTION on every platform

The island-facing contract is identical everywhere: *"ask a peer that can reach
this device to ring it."* On Android the peer is any island. On iOS the set of
peers that can reach the device happens to have exactly one member — the
publisher's island. **Same protocol, same discovery, same ring-ticket, same
signature. A degenerate case, not a special case.**

Better than a tower, because the degeneracy is *Apple's*, is visible in the data,
and evaporates the moment a platform allows it. Andy's delegation table holds it
either way; on Apple it resolves to one entry.

**Cheapest validation: build it Android-first.** Proves the peer-ringing protocol
end to end — discovery, tickets, island signatures — with no tower, no enclave,
and no Apple problem to solve at all.

---

## 5. Whatever the ringer is, it should hold NO policy

Apply the DNS root lesson (§3.2): delegations, not answers. Every piece below
already exists in this system — no new trust primitive is required.

- The device, at registration, issues a **ring-ticket**: an opaque capability
  bound to (device, home island). Travels device → island.
- The island signs ring requests with **`ISLAND_SIGNING_SEED`** — the Ed25519
  identity it already has from crucible-09.
- The ringer verifies ticket + signature and forwards. That is all it does.
- Payload stays `event_id_only`, so the ringer never learns content.

**Be precise about what this buys:** islands can no longer ring each other's
users — the actual threat. It does NOT stop the credential-holder ringing anyone
with anything; that is irreducible. Confidentiality yes, ring-authority no.

### The seam already exists

`domain/apns.py` exposes four functions, with exactly one caller of `send`:

```
apns.send(device_token, payload, collapse_id=)   <- one caller (push_service.py:377)
apns.is_configured()
apns.aclose()
apns.reset_for_tests()
```

`push_service` holds all policy (who may be woken, payload shape, reaping);
`apns.py` holds only "hand this to Apple and report what it said". **Swapping
direct-to-Apple for a relay client, or for a peer-ring client, is one module.**
So this decision is cheap to defer — which is the main reason to defer it.

### Do not regress the reaping rule

`apns.py` reaps ONLY on `410 Unregistered`, deliberately NOT on `400
BadDeviceToken` — see the docstring at `domain/apns.py:212-240`. `BadDeviceToken`
is also what Apple returns for a valid token sent to the WRONG ENVIRONMENT, so a
naive reaper would delete the entire `device_tokens` table on one mis-set flag.
This is what makes wrong-environment sends non-destructive, and therefore what
makes any try-the-other-host fallback safe to build.

---

## 6. UNVERIFIED PREMISES — run these before any Cast

**A `/crucible` fed these unverified would harden a frame none of its adversaries
can see is wrong.** N reviewers sharing a premise corroborate each other, not
reality. This is the #2634 shape, and the reactive-deploy crucible burned a full
ceremony before Temper found what the Fold had laundered past.

1. **Andy's registrar-of-registrars.** Everything in §4.3 and §4.4 about
   delegation tables rests on a one-sentence summary ("like DNS, a set of servers
   around the world"). No spec read, Andy not asked. **Highest-yield, cheapest
   falsifier — and it rides along with the already-open #3247 / #3157.**
2. **Can an APNs auth key be restricted to a single bundle id?** Believed yes
   (added by Apple in recent years) but unconfirmed. Narrows blast radius; does
   NOT provide per-operator scoping, so it does not change the conclusion — but it
   moves a real number. Apple docs, ~5 minutes.
3. **UnifiedPush against a real Flutter app.** §2's "buildable now on Android" is
   asserted, not costed. Unknown how it interacts with the existing notification stack.
4. **Enclave option (§4.4.3).** Nitro/SEV-SNP named with more confidence than
   earned for this specific use.
5. **Does the island's actual payload match design 07's `event_id_only`
   envelope?** Not checked.

---

## 7. Recommendation as of 2026-08-23

**Nothing structural now.** One operator, two islands, and the app publisher is
the same person. A tower today is a new deployable on the critical path of every
notification, bought with zero current benefit — and design 07 agrees ("today
there's one island and it rings its own bell — simple").

Two things that cost nothing and keep every door open:

1. **Do not copy the `.p8` to enspyr.** One credential-holder is a property worth
   keeping by default rather than re-establishing later. Under every model above,
   one credential = one publisher, and enspyr is not one.
2. **Keep the seam clean** (§5). It already is.

**Forcing function — the trigger that sends this to `/crucible`: the first island
operator who is not Nick.** That is when the question stops being theoretical, and
also when there is most temptation to just send someone the key because they are a
friend and it is Friday. Verify §6 first, then run the ceremony.

---

## 8. Provenance and a caution about this document

Written by Claude from a single conversation on 2026-08-23 08:00–08:50 AEST,
immediately after shipping the APNs credential to imagineering.

Four architectures were generated in about twenty minutes, each more elegant than
the last, with enthusiasm climbing. That pattern — escalating better proposals
with no falsifier run — is the tell for reasoning inside a build frame rather than
mapping territory. §6 exists because of it. **Treat §4 as a generated option
space, not as findings.**

The product question underneath, which is Nick's and not an agent's: *is "aiko
chat is a protocol with multiple client publishers" a direction you want?* If yes,
push stops being special and dissolves into an already-solved shape (§4.3). If no
— one app, and it is yours — then the tower is simply a component you run, and
the honest move is to document it as a platform-forced centre and stop trying to
design it away.
