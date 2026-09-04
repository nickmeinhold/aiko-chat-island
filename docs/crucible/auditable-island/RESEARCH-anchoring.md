# RESEARCH — substitutes for observer density at very small N

Scope: given that split-view detection by gossip is density-dependent and dead at N=2,
what else buys non-equivocation, and what does it cost? Tags: **[DEPLOYED]** = observed
in a shipped system, **[PAPER]** = claimed in literature, **[INFERENCE]** = mine.

---

## 0. The headline

**Density is not the only source of cross-client agreement. The general mechanism is *a
shared reference point the island does not control*.** Gossip synthesises one out of many
clients; anchoring borrows one from a public chain; witness cosigning rents one from a
handful of third parties. They are the same mechanism at different N and different
admission cost — not three unrelated options. [INFERENCE]

And the orchestrator's suspicion — "an anchor proves append-only-ness, not
non-equivocation" — is **refuted as stated, but only because of a property that is easy
to lose in implementation**. Detail in §1. The honest correction is: *anchoring proves
non-equivocation at anchor granularity, and proves nothing at all inside the epoch
between anchors*, which for Keybase is up to an hour of freely equivocable history.

---

## 1. Public bulletin board / anchoring

### Does it prove non-equivocation?

Yes — and this is a *formal* result, not a vibe. Catena (Tomescu & Devadas, IEEE S&P
2017, https://eprint.iacr.org/2016/1062) builds a log as an OP_RETURN transaction *chain*
on Bitcoin: each root commits by spending the previous output. **"If a log server wants to
equivocate it has to double spend a Bitcoin transaction output"** — so forking the log is
exactly as hard as forking Bitcoin. [PAPER] The paper's own framing is the useful one:
Bitcoin's double-spend prevention *is* a non-membership proof, i.e. "no other root was
published at this height".

The load-bearing detail is that the anchor must be a **chain**, not a scatter of
independent timestamps. A bare `OP_RETURN(root)` per epoch, unlinked, lets the operator
publish two roots in the same block and show each client the transaction that suits it —
that degenerates to append-only-ness only. Catena's spend-linkage is what forces
*at most one* root per epoch. [PAPER + INFERENCE]

Keybase [DEPLOYED] does this at the application layer and says so plainly: since
2020-01-20 it publishes the Merkle root to Stellar and asserts **"Thanks to Stellar, we
are unforkable"** — Alice and Bob consulting the chain see the same value, so a server
fork is detectable
(https://keybase.io/docs/server_security/merkle_root_in_stellar_blockchain). It published
into Bitcoin every 12h from 2014-06-16
(https://book.keybase.io/docs/server/merkle-root-in-bitcoin-blockchain).

### The three costs, and they are all real

1. **Epoch blindness.** Keybase's own doc: *"Keybase publishes to the Stellar Blockchain
   only about once an hour, despite updating itself about once a second."* [DEPLOYED]
   Between anchors the server's head is an unwitnessed promise. Any client that accepts a
   newer-than-anchor head — which every interactive client must, or messaging stops for an
   hour — is back to a first-person guarantee for that window. Bitcoin at 12h was worse.
   For a key-binding log this is the whole attack window: a key swapped and un-swapped
   inside one epoch never reaches the chain at all, *unless* the log is forced to include
   the fraudulent leaf in the next anchored root — which it is only if the client
   *retains* the head it was shown and re-checks consistency later. **The anchor does not
   work without client-side retention plus deferred re-check.** [INFERENCE]
2. **The client must read the chain independently.** If the island serves the anchor, the
   island can serve a fake anchor. A real Stellar/Bitcoin light client on a phone is
   heavy; a block-explorer API is a third party you now trust — the admission problem
   re-entering by the back door, though a *much* cheaper one (an explorer that lies about
   a public chain is caught by anyone). [INFERENCE]
3. **Money and liveness.** Per-anchor transaction fee, a hot wallet on a box whose
   operator is the adversary, and a funding failure that looks identical to censorship.
   [INFERENCE]

### Non-chain bulletin boards

- **A public git repo (GitHub) of signed checkpoints.** Cheap, zero-crypto-asset, human
  auditable, and GitHub is a widely-witnessed medium the island does not control. But
  GitHub is *mutable by the pusher* (force-push) and by GitHub; it gives no
  at-most-one-root-per-epoch property unless clients also witness the repo. It is
  anchoring's *ergonomics* with none of its guarantee. [INFERENCE] Useful as a
  transparency/monitoring convenience, not as a non-equivocation mechanism.
- **Existing transparency-log ecosystems (CT, Sigstore/Rekor, Go sumdb).** These do not
  accept foreign payloads for anchoring. Sigstore/Rekor accepts *signed artefacts*, so an
  island could in principle log its checkpoints into Rekor as artefacts — but Rekor is a
  single operator (Linux Foundation) with its own split-view exposure, so this reduces to
  "trust Sigstore's operator", not to a public bulletin board. [INFERENCE]
- **The Go checksum database model** is instructive for what it *doesn't* do: every `go`
  command verifies inclusion and consistency against **its own last-seen tree head**
  (https://blog.golang.org/module-mirror-launch) [DEPLOYED] — i.e. Go shipped the
  first-person guarantee (§3) at ecosystem scale, and layered witnesses on afterwards. It
  is the existence proof that the honest floor is a shippable product, not a consolation
  prize.

---

## 2. Witness cosigning — the strongest candidate, and cheaper than expected

The spec is C2SP `tlog-witness` (https://c2sp.org/tlog-witness@v1.0.0, source
https://github.com/C2SP/C2SP/blob/main/tlog-witness.md) with cosignature format
`tlog-cosignature` and client policy `tlog-policy`. Sigsum
(https://git.sigsum.org/sigsum/plain/doc/design.md) is the deployed system built entirely
around it.

**What a witness stores: O(1).** *"For each log, uniquely identified by its origin line,
the witness is only required to keep track of the latest checkpoint it observed and
verified."* [DEPLOYED spec] It never sees log *contents* — only `old size`, a consistency
proof, and the new checkpoint. Filippo's keyserver write-up puts the asymmetry crisply:
*"the log provides the O(log N) consistency proof when requesting a cosignature, and the
witness only needs to store the O(1) latest checkpoint"*
(https://words.filippo.io/keyserver-tlog/). [DEPLOYED]

**This kills the CT-gossip privacy deadlock for this project.** CT gossip died because
telling someone what you have *seen* leaks who you talked to. A witness sees only tree
heads and sizes — it learns the island's *rate of activity*, never any binding, any
identity, or any edge of the social graph. Against this project's decided constraint (the
island must learn neither who is friends with whom nor who is calling), a witness is
strictly safer than the island itself. [INFERENCE, on a [DEPLOYED] spec]

**What it costs to run:** an HTTP service with a SQLite file and a signing key
(transparency-dev/witness, Filippo's `litewitness`). It genuinely is an unattended cheap
service. There is a shared network — the ArmoredWitness deployment, **15 devices with
independent custodians**, already witnessing Go SumDB, Sigstore and Android Binary
Transparency (https://blog.transparency.dev/can-i-get-a-witness-network) [DEPLOYED]; and
Sigsum's witness set, with a documented path to running one on a Tillitis TKey
(https://www.tillitis.se/guide-tkey-sigsum-lab-witness-howto/).

**Statefulness is the sharp edge.** The spec: *"checking the old size against the latest
checkpoint and persisting the new checkpoint must be performed atomically."* A witness
that loses its DB and restarts at size 0 will happily cosign a forked branch. A witness is
therefore exactly as fragile as the thing it is protecting — see §5.

**How many are needed?** The guarantee is a client-side quorum rule (`tlog-policy`):
"k of N". Sigsum's stated assumption is *"at most a threshold of independent witnesses stop
following protocol"*, and the honest consequence is *"an attacker can at best deny
service"*. [DEPLOYED doc] The strong statement is: **k=1 already buys everything, if the
one witness is honest and independent.** A single non-colluding witness that has cosigned
head H makes it impossible to show any other client a head inconsistent with H (they will
refuse it for lack of a cosignature). k>1 buys *resilience*, not a different property.
[INFERENCE from the spec's semantics] So a two-island federation is not blocked by having
too few *islands* — the witnesses need not be islands.

**Is a witness quorum just the admission problem again?** Partly, and the difference
matters. Yes, someone must decide the witness list. But (a) it is a **client-side policy
pinned at install/update time**, not a directory the island serves — the island cannot
add its own witness; (b) a witness has no power to *approve* anything, only to *refuse* —
a captured witness can withhold a cosignature (DoS) or cosign a fork, and cosigning a fork
is only harmful if *every* witness in the quorum does it; (c) the trust is
non-discretionary — a witness runs a fixed algorithm on public data, so it can be
audited by anyone who has both checkpoints. This is materially weaker trust than CONIKS'
third-party auditor, which had to be given a role in the identity system.

The residual, and it is a real one: **the client's witness list is a small hardcoded set
of names, shipped by the same people who ship the client.** For a federation whose whole
premise is "the operator is the adversary", the app vendor is now a trust root. Say that
out loud in the design rather than discovering it at Temper. [INFERENCE]

---

## 3. The honest floor — confirmed, and it has a name

If there is no anchor and no witness, the client still gets: **the island cannot show *me*
two mutually inconsistent histories, and cannot retroactively edit a history it has
already shown me.** Confirmed. The mechanism is retention of the last-seen checkpoint plus
a consistency proof on every subsequent one — exactly what every `go` command does
[DEPLOYED].

**The literature name is fork consistency / fork-linearizability**, from SUNDR (Li,
Krohn, Mazières, Shasha, OSDI 2004, https://www.usenix.org/conference/osdi-04/secure-untrusted-data-repository).
SUNDR *"guarantees fork consistency, so clients can detect any integrity or consistency
failures as long as they see each other's file modifications"*. Cachin et al. establish it
as **"the strongest consistency notion among the clients that can be achieved ... without
client-to-client communication"** (https://csaws.cs.technion.ac.il/~shralex/fp147-cachin.pdf).
[PAPER] That is a precise theorem-shaped answer to "what is the floor": *fork consistency
is provably the ceiling of what N=1 observation can give*. There is nothing better to find
without a shared reference point.

The properly-stated pair:

- **Stops:** retroactive edit; deletion of a binding I have seen; showing me a key today
  and denying it tomorrow; any inconsistency between two things shown *to the same
  client*.
- **Does not stop:** showing you a different key for me than it shows me, forever, from
  first contact, undetected. No amount of my own checking touches this.

One under-sold consequence worth carrying into the design: **a fork is permanent**. Once
the island forks, it must maintain both branches for the lifetime of both clients, and any
single artefact that crosses the branches (a message receipt, a shared channel, one
consistency proof requested at the wrong moment) exposes it. Equivocation is not a
one-off lie, it is an unbounded, ever-growing lie. That is a genuine deterrent even at
N=2 — it just is not a *detection* guarantee. [INFERENCE from fork-consistency semantics]

---

## 4. First contact

Nothing fixes it cryptographically. The BRSKI framing is the blunt one: *"The secure
establishment of a key infrastructure without external help is also an impossibility"*
(RFC 8995 lineage, https://www.rfc-editor.org/rfc/rfc8995.html). [PAPER] Everything on
offer is a way of importing external help:

- **Domain-bound keys / DNSSEC+DANE.** The island already has a domain
  (`chat.enspyr.co`). Binding the island's log key to its DNS name via DANE moves first
  contact from "trust whatever the box says" to "trust the registrar + DNSSEC chain". That
  is a *different* and considerably better-policed adversary than the box operator,
  because it is not the box operator. Cost: DNSSEC on the zone, and the honest admission
  that the registrar can lie. [INFERENCE]
- **`.well-known` over HTTPS.** Reduces to WebPKI, i.e. to CT — which is a real,
  functioning, high-density transparency ecosystem the island can free-ride on. A key
  published in a cert-pinned `.well-known` inherits CT's density. Under-rated; cheap.
  [INFERENCE]
- **Anchoring helps first contact only weakly**: a fresh client with no prior root can at
  least check that the root it is being handed *appears on the chain*, which is far
  stronger than nothing. This is the one place anchoring beats witnessing on ergonomics,
  since the chain has a public history the client can walk backwards. [INFERENCE]
- **Human ceremonies stay off the trust path**, per the project's own constraint (Turner
  2023; Schröder 2016). Nothing found contradicts that.

---

## 5. Cost of being wrong — the finding I would flag hardest

**Deployed systems do not distinguish "the log broke" from "the log lied". They do not
try.** Andrew Ayer's catalogue of CT log failures
(https://www.agwa.name/blog/post/how_ct_logs_fail) lists, over ~7 years:
excessive downtime; a private key reused across two logs; **an append-only violation
caused by a botched backup restore (2017)**; an operator that simply disappeared
(a Chinese blockchain company, 2017); logs that failed to include submitted certificates
(twice, 2018); **a single bit flip corrupting entry 65,562,066 of Yeti (2021/2022)**; and
one genuinely compromised key (2020). [DEPLOYED] Read the list: **essentially all
operational, none an equivocation attack.** The detection signal is identical in every
case — a consistency or inclusion proof fails — and the response is identical too: the
log is moved to **"retired"**, its historical SCTs still honoured, and the ecosystem
carries on *because Chrome's policy requires SCTs from multiple independent logs*.

That last clause is the whole answer, and it is the bad news for a two-island federation.
**CT survives indistinguishability by having redundancy to spend.** The prior research
already established that a two-node federation has none. So:

- A witness quorum on a one-box island converts every botched restore, every disk
  corruption, every `alembic` misstep into a **cryptographic accusation of equivocation
  against the operator**, with no mechanism to say "it was a bit flip". [INFERENCE]
- The only known mitigations are *ex-ante*: make the log's state cheap to reconstruct
  (append-only file, replayable from primary tables, backed up separately from the DB),
  and give the operator a documented, public, signed **"I broke it, here is the new
  origin"** path — a deliberate log *rotation*, which is what CT's "retire and re-issue"
  amounts to. A rotation that clients surface loudly is honest; one they accept silently
  voids the entire mechanism. [INFERENCE]
- Corollary worth stating in the design doc: **the same fragility applies to the witness**
  (§2, atomic state). A witness that loses its DB and a log that loses its DB produce the
  same class of incident. Two fragile single-box services do not add up to redundancy.

---

## 6. What I would take into the design

1. Anchoring and witnessing are the same move; witnessing is the cheaper, more private,
   lower-latency, no-money version of it, and it is a live spec with a live shared
   network. If exactly one substitute is chosen, choose witnesses.
2. Neither is worth building unless the client **retains its last-seen checkpoint and
   re-checks consistency later** — that is the piece all three mechanisms share, and it
   is also the entire honest floor on its own. Ship it first; it has independent value
   (§3) and it is a prerequisite for everything else.
3. State plainly in the design that a witness list makes the *app vendor* a trust root,
   and that operational failure is indistinguishable from attack. Both are true and both
   are survivable; neither should be discovered at Temper.
