# Change: Identity and trust without a certificate authority

Status: Draft for discussion. Not a decided spec. Backed by two
adversarially-verified research passes (formal theory + a survivor autopsy of
shipped systems); load-bearing claims are cited inline. Spec deltas and a task
breakdown are deferred until the direction is agreed.

## Why

An aiko island has to answer three questions about identity at runtime:

1. **Continuity** — is this the same principal that authored the last message?
2. **Introduction** — I have never seen this island or user before; should I
   trust the key they present?
3. **Recovery** — the private key is lost or compromised; how does the principal
   get back?

A Certificate Authority is sold as the answer to all three. It is really the
answer to a fourth question aiko does not ask: **name-binding**, "does this key
belong to real-world-person-X?" A CA bundles name-binding with
continuity/authority and sells them as one product. aiko needs continuity and
authority; it has **deliberately dropped name-binding**. Being robots-first-class
makes proof-of-personhood a category error, and the sybil-resistance design
(Design 09) defends per-island on reputation and economic cost, not identity
gates. So a CA whose distinctive value is attesting "this key is really Andy" is
machinery for a corner of the design space aiko already walked away from.

This is not a fringe stance; it is SPKI/SDSI, written down in 1999 by Ellison,
Frantz, Lampson, Rivest, Thomas and Ylonen
([RFC 2693](https://www.rfc-editor.org/rfc/rfc2693.html)): "PRINCIPAL: a
cryptographic key", and "A basic SPKI certificate defines a straight
authorization mapping: authorization -> key ... The data needed for that decision
is almost never the spelling of a keyholder's name." SDSI's rule, "all names are
local to some principal", means there is no global namespace and therefore no
global root to run. The premise X.509 rests on, that knowing a name means knowing
an identity, is what Ellison called the "Walton's Mountain Assumption" and judged
a failure. aiko's key-as-identity commitment maps exactly onto SPKI's "the
principal is a key."

The honest counter-cost is stated up front, not buried: going CA-free costs
scalable introduction-of-strangers, and no surveyed system solved that without a
directory. That constraint shapes the proposal rather than undermining it.

## What Changes

- **Adopt a three-layer trust model** in place of any global identity CA:
  key-as-principal, a petname layer for names, and a per-island auditable
  key-transparency (KT) log as the trust anchor.
- **Keep** the existing key-as-identity core (the `signing_keys` roster, the
  message-signing envelope, the proof-of-possession handshake) as Layer 1
  unchanged.
- **Add** a petname layer over the per-island roster for memorable local names,
  scoped as usability only. Any verification ceremony is optional and never the
  trust boundary.
- **Add** a per-island auditable KT log (CONIKS-lineage) as the trust anchor,
  with cross-island gossip of signed log roots over the existing mesh.
- **Validate** the guardian-quorum recovery (Design 05), whose k-of-n threshold
  and time-locked veto already match the only at-scale shipped social-recovery
  pattern (smart-contract wallets); adopt the wallet framing as external evidence
  the design is on the right track.
- **Reframe** the federation claim honestly: not "no central point" but "a
  per-island auditable directory and no global trusted root."
- **Reframe** Andy's proposed "softened CA" (a CA signing a key that grants
  self-signing authority) as an SPKI authorization-delegation primitive anchored
  in the KT log, rather than as identity certification.

## Impact

- Affected specs: `03-auth-on-the-bus`, `04-passkey-first-identity`,
  `05-social-recovery`, and the app-repo sybil-resistance design (Design 09).
- Affected code (island): the `signing_keys` roster and message-signing envelope
  (#1816); the proof-of-possession handshake (#1974); recovery service and the
  guardian-quorum flow (Design 05, #1914); a new key-transparency-log capability;
  the gossip mesh (adds signed-root distribution).
- Affected code (app): key/identity verification UX (petnames + optional
  confirmation ceremony), which must not present a raw-fingerprint comparison as
  the trust gate.
- Non-goals: global naming, proof-of-personhood, transitive key-signing as a
  stranger-introduction mechanism.

## Design

### Context

No single identifier can be simultaneously global, secure, and memorable (Zooko's
Triangle). A petname system resolves this at the *system* level, holding all
three properties across distinct name-types even though no one identifier does
(Stiegler;
[Ferdous & Jøsang](https://www.researchgate.net/publication/221426438_Security_Usability_of_Petname_Systems)).
That is the shape of the stack below.

Two research passes back this design. The first established the formal precedent
(SPKI/SDSI, petnames, CONIKS) and the failure record (PGP web-of-trust). The
second was a survivor autopsy of systems that actually shipped at scale, which
both validated the trust-log layer and corrected two points the theory alone got
overconfident about (see Decisions 2 and 3, and Risks).

### Goals and non-goals

- **Goals:** answer continuity, introduction, and recovery for a federated
  key-as-identity network without a global trusted root; keep the trusted parties
  that remain **per-island and auditable**; design recovery in from the start.
- **Non-goals:** binding keys to legal identity; global unique names;
  eliminating every trusted party (the shipped record shows this is not an
  achievable outcome, only an auditable one).

### Decisions

#### Decision 1: The key is the principal (SPKI/SDSI)

Keep what aiko already has. The public key is the identity; a signature over a
challenge (proof-of-possession) is the entire authentication story. No third
party vouches for a binding that is *definitional*, not asserted: there is no
"you" behind the key that the key could fail to match.

- **Rationale:** directly precedented by SPKI/SDSI (RFC 2693); it is the layer
  aiko already built and the one every surviving key-as-identity system (SSH,
  Signal, Nostr, Bluesky) shares.
- **Alternatives rejected:** an X.509-style identity certificate, which solves
  name-binding (renounced) and needs a global root (unwanted).

#### Decision 2: Petnames for memorability, not for trust

Layer a per-user, locally-scoped memorable label ("petname") over each pinned key
in the roster, so humans have a name to read without a global namespace.

The load-bearing constraint from the shipped record: **do not make human
verification the trust defense.** At Signal/WhatsApp's real 60-digit safety-number
length, about one third of users blind-accept a MITM'd key
([Turner et al., ARES 2023](https://arxiv.org/pdf/2306.04574)); 21 of 28
technically-sophisticated users failed to detect a live MITM, nearly half of them
*confident* they had verified
([Schröder et al., NDSS 2016](https://www.ndss-symposium.org/wp-content/uploads/2017/09/09-when-signal-hits-the-fan-on-the-usability-and-security-of-state-of-the-art-secure-mobile-messaging.pdf)).
Verification is measurably worse remotely than in person
([Shirvanian et al.](https://arxiv.org/pdf/1707.05285)), and an island federation
verifies across the network by construction.

- **Rationale:** petnames resolve the memorability corner of Zooko's Triangle
  without inventing a global name, and keep the human out of the per-connection
  trust decision that safety-number UX loses.
- **Implementation note:** if a confirmation ceremony is offered, use
  human-comparable word-lists plus a traffic-light status (pEp trustwords /
  Signal safety-number style, which redesign studies push to ~90% completion),
  never raw fingerprint comparison.
- **Alternatives rejected:** safety-number / fingerprint comparison as the trust
  gate (fails at scale, worse remotely); a global human-readable nickname
  registry (reintroduces global naming).

#### Decision 3: A per-island auditable key-transparency log

This is the trust anchor. An island stays authoritative for name -> key bindings
*within its own namespace*, but unlike a certificate (which presents only an
authoritative signature) each binding carries a **cryptographic proof of
consistency** against an append-only Merkle log. Each user monitors only their own
binding (tens of kB/day even against a billion-user provider). Provider
equivocation, showing different keys to different observers, becomes
cryptographically self-incriminating; cross-island gossip of signed log roots is
what makes a split view detectable.

- **Rationale:** the survivor autopsy upgraded this from theory to field-proven.
  CONIKS-lineage KT ships at ~2-billion-user scale: WhatsApp's Auditable Key
  Directory (CONIKS -> SEEMless -> Parakeet, Cloudflare as independent auditor,
  open-source implementation,
  [engineering.fb.com](https://engineering.fb.com/2023/04/13/security/whatsapp-key-transparency/))
  and Apple iMessage Contact Key Verification ("uses the verifiable, log-backed
  map data structure described in CONIKS", >2 billion log entries/week,
  [security.apple.com](https://security.apple.com/blog/imessage-contact-key-verification/)).
- **Production lesson to carry (Apple's Signed Mutation Timestamps):** vanilla
  CONIKS gates a new key's usability on merge epochs. Apple added an auditable
  "promise to merge" (analogous to Certificate Transparency's SCTs) plus a
  client-enforced 48-hour maximum-merge-delay, so a new key works instantly and
  the promise is audited later. Plan promise-then-merge, not epoch-gated
  availability.
- **Alternatives rejected:** pure sovereign key-as-identity with no log (Layer 1
  alone; ships without recovery, cannot introduce strangers); a classic CA
  (global root, name-binding).

#### Decision 4: The wallet pattern confirms the recovery design we already have

The uncomfortable finding: **no messaging system has shipped social-graph recovery
at scale.** Nostr's npub has no native rotation; Bluesky layered DIDs and a
directory *over* keys because a lost key meant permanent account loss. The only
at-scale shipped precedent for m-of-n social recovery is smart-contract wallets:
Argent uses a **ceil(N/2)-of-N guardian threshold** plus a **mandatory 48-hour
on-chain time-locked cancellation window** the owner can veto with
([Argent](https://support.argent.xyz/hc/en-us/articles/360007338877-How-to-recover-my-wallet-with-guardians-onchain-complete-guide)).

aiko's guardian-quorum recovery (Design 05) **already has both halves**: a k-of-n
guardian threshold and a time-locked veto that opens a pending state existing
devices can cancel. So this is not a change to make; it is external validation
that the Design 05 choices land on the same pattern the wallet world converged on
independently.

- **Rationale:** the time-locked veto is what makes a colluding-quorum attack
  non-instant and owner-vetoable; the wallet precedent confirms that is the right
  property, not an over-engineering. It also suggests the two open Design 05 dials
  (choice of k/n, veto duration) can be tuned against a shipped reference point
  rather than guessed.
- **Caveat:** Argent has since rebranded and added an off-chain recovery product;
  the ceil(N/2) + 48h mechanism is confirmed, the productisation has moved on.

#### Decision 5: Andy's "softened CA" is SPKI delegation, anchored in a log

Andy proposed a CA that signs a key which itself grants you authority to sign your
own keys. This design **names rather than rejects it**: that construction is SPKI
authorization delegation, a principal delegating a capability (self-signing
authority) to a key via a signed statement.

- **Rationale:** framing it as capability delegation (not identity certification)
  keeps it inside SPKI's "authorization -> key" model and out of the X.509 trap.
  And once a key can sign its own keys, a CA's signature is cryptographically
  inert after enrollment; its only residual job is introducing a stranger, which
  a per-island KT log plus the directory does auditably. So the softened CA,
  taken one step further, replaces its own signing root with an auditable log. A
  per-island KT log **is** "a CA you never have to fully trust."

### Risks and trade-offs

- **Introduction-of-strangers does not scale without a directory.** Transitive
  social trust caps at about two hops in practice
  ([Finney](https://nakamotoinstitute.org/library/pgp-web-of-trust-misconceptions/));
  PGP's web-of-trust died on that plus a brutal verification UX. No surveyed
  survivor escaped the two-hop ceiling without a central directory/log. aiko's
  answer must be reputation + economic cost (Design 09) plus directory/gossip
  discovery, stated plainly rather than implying key-signing carries strangers.
- **"No central point" is not a shipped outcome.** Every surviving KT and
  recoverable-identity deployment (WhatsApp AKD, Apple IDS+KT, Bluesky
  plc.directory) reintroduced a central directory plus log operator and made it
  auditable rather than eliminating it; Bluesky's own team calls its directory's
  centralisation a debt. The achievable, honest claim is "a per-island auditable
  directory and no global trusted root": decentralisation by auditability and
  locality, not by elimination. Prefer per-island sovereignty (a `did:web`-style,
  self-hosted stance) over one shared directory.
- **Key loss is real and its rate is unmeasured.** We know irrecoverable-key
  identity hurts users; we have no production loss-rate data for Nostr or Bluesky.
  Design recovery in from the start and treat the recovery-vs-centralisation
  tradeoff as explicit.
- **Evidence caveats.** The usability numbers come from controlled lab studies
  (n=28, n=162): the direction is robust and independently replicated, exact
  field percentages are not measured. KT scale figures are vendor-reported. KT
  centralisation findings describe 2023 launch state and are evolving.

### Open questions

1. Is there any shipped, at-scale social-graph recovery outside smart-contract
   wallets? The autopsy found none in messaging or identity systems.
2. What is the real-world key-loss / lockout rate for Nostr and Bluesky in
   production? Architecture is confirmed; operational pain is unquantified.
3. What split-views has Certificate Transparency gossip actually caught, and how?
   This would directly inform aiko's cross-island KT gossip design.
4. Does the KT log run per island, or is there a shared federated log with
   per-island namespaces? (Locality vs cross-island auditability tradeoff.)

## References

- [RFC 2693: SPKI Certificate Theory](https://www.rfc-editor.org/rfc/rfc2693.html) (Ellison, Frantz, Lampson, Rivest, Thomas, Ylonen)
- [SDSI slides, Rivest & Lampson](https://people.csail.mit.edu/rivest/pubs/RL96.slides-rsalabs96.pdf)
- [Zooko's Triangle / petname systems, Stiegler](https://www.financialcryptography.com/mt/archives/000499.html); [Ferdous & Jøsang, Security Usability of Petname Systems](https://www.researchgate.net/publication/221426438_Security_Usability_of_Petname_Systems)
- [CONIKS: Bringing Key Transparency to End Users, Melara et al., USENIX Security 2015](https://www.semanticscholar.org/paper/CONIKS:-Bringing-Key-Transparency-to-End-Users-Melara-Blankstein/507067837b06f0bd2504a63e7af6a8b947e93152)
- [WhatsApp Key Transparency](https://engineering.fb.com/2023/04/13/security/whatsapp-key-transparency/)
- [Apple iMessage Contact Key Verification](https://security.apple.com/blog/imessage-contact-key-verification/)
- [Bluesky did:plc method spec](https://github.com/bluesky-social/did-method-plc/blob/main/README.md)
- [PGP Web of Trust misconceptions, Hal Finney](https://nakamotoinstitute.org/library/pgp-web-of-trust-misconceptions/)
- Verification UX: [Schröder et al. 2016](https://www.ndss-symposium.org/wp-content/uploads/2017/09/09-when-signal-hits-the-fan-on-the-usability-and-security-of-state-of-the-art-secure-mobile-messaging.pdf), [Turner et al. 2023](https://arxiv.org/pdf/2306.04574), [Shirvanian et al.](https://arxiv.org/pdf/1707.05285)
- [Argent guardian recovery](https://support.argent.xyz/hc/en-us/articles/360007338877-How-to-recover-my-wallet-with-guardians-onchain-complete-guide)
