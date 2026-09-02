# RESEARCH — federated / self-hosted key transparency

Filling the coverage hole in `RESEARCH-priorart.md`: that pass examined only
CENTRALISED KT (CONIKS-as-deployed, WhatsApp AKD, Signal, Apple CKV). This pass
asks who has shipped KT in a **federated or self-hosted** setting, and what broke.

Every claim tagged `[DEPLOYED]` (shipped/measured), `[PAPER]`, or `[INFERENCE]`.
Secondary-source claims say so **inside the claim**.

---

## Verdict first

1. **Per-server roots with no global root is the STANDARDISED architecture, not a
   novelty.** IETF KEYTRANS `draft-ietf-keytrans-architecture-05` §5.3 describes
   exactly this for the federated case and tells servers *not* to combine logs.
   The per-island design is on the mainline. `[PAPER]`
2. **Nobody has shipped it.** Every deployed KT system is one operator, one log,
   one namespace. The federated half of the design space has drafts and papers
   and zero production systems. `[DEPLOYED absence]`
3. **The Keybase autopsy is the most useful thing here, and it is not about
   cryptography.** Their High-severity finding was that an *operational scar* —
   a hardcoded blacklist of chain links their own server bugs had written into
   the immutable record over 3.5 years — became a server-controlled verification
   bypass. The append-only property is what *created* the vulnerability.
4. **The auditability was, as far as any measurable signal shows, never used.**
   See §1(e). This is the finding that should move the design.

---

## 1. Keybase — the deep dive

### (a) Structures and what a client verified `[DEPLOYED]`

Per-account **sigchain**: an ordered list of signed statements, each link signed
by a currently-delegated key, carrying a seqno and the hash of the previous link
(keybase.io/docs/sigchain). All sigchains roll into a server-maintained global
**Merkle tree**; the server signs a root (an "STR"-equivalent) on every change.

A client replays a user's subchain from the eldest key forward, applying
delegations/revocations to an internal model and checking each statement was
signed by a key live *at that point in time*, then checks the leaf against the
signed Merkle root. Measured now: the root API is live and moving —
`GET https://keybase.io/_/api/1.0/merkle/root.json` returned `seqno 27753951`,
`ctime 2026-09-01T23:56:37Z` (fetched 2026-09-02). `[DEPLOYED, measured]`

**Account reset breaks the chain by design.** A reset starts a *new* sigchain
for the same username with a new eldest key; the client "starts playback at the
most recent link whose `eldest_kid` matches the one in the Merkle tree"
(keybase.io/docs/sigchain). So the append-only record has a sanctioned
discontinuity — the transparency log does not prevent identity replacement, it
only makes it *visible*, and only to someone looking. `[DEPLOYED]`

### (b) What Bitcoin anchoring bought `[DEPLOYED]`

Since 2014-06-16 Keybase hashed its Merkle root into Bitcoin, ~every 12h, from a
fixed address (book.keybase.io/docs/server/merkle-root-in-bitcoin-blockchain).
Purpose stated in their own docs: defeat a *fork* — a compromised server showing
Alice and Bob different histories. It buys **non-equivocation against a
server-side attacker**, nothing else. They later moved to Stellar (~hourly),
citing cost/latency; the Bitcoin page is now marked superseded. `[DEPLOYED]`

I could not find a published cost figure. `[coverage gap]`

### (c) What went wrong operationally — the important part `[DEPLOYED]`

NCC Group, *Protocol Security Review: Keybase*, v1.3, 2019-02-27 (public PDF on
keybase.io). 3 consultants, 9 person-weeks, Sept 2018, retested Feb 2019.
**5 findings: 1 High, 3 Medium, 1 Low, 0 Critical.** All fixed.

- **NCC-KB2018-001 (High) "Signature ID Blacklist Bypasses Verification"** — the
  autopsy. Quoting the report: *"some subchains contain chain links with invalid
  data. These were accepted by the server due to various server bugs between
  March 2015 and September 2018 and incorporated into the immutable record. Once
  the bugs were identified, the invalid chain links could not be removed, and
  clients needed to be modified to prevent acceptance of the invalid links. This
  is done by hardcoding the signature ID in a blacklist."* The blacklist keyed on
  a **server-supplied signature ID not covered by the chaining hashes**, so a
  compromised server could relabel any *legitimate* link — including a key
  revocation — as blacklisted, and the client would skip it while sigchain
  validation still passed. Consequence: a revoked, leaked key could be made to
  look live. Fixed by keying exclusion on (user ID, seqno, link ID).
  **The immutability of the record is what forced the scar, and the scar was the
  hole.** The same pattern existed in a `hardcodedResets` array.
- **NCC-KB2018-004 (Medium) "Ambiguity in Signature Payload Interpretation"** —
  V1 (JSON) vs V2 (msgpack) payloads, with the version indicator not covered by
  the signature: a polyglot byte string could verify under two readings,
  *"giving two victim users inconsistent views under the same Merkle tree"* —
  i.e. equivocation **surviving** the Merkle tree. A format-versioning bug
  defeating the anti-forking machinery.
- Others: msgpack/JSON nesting DoS crashing clients and servers (Medium), weak
  password policy (Medium), KBFS block forking (Low).

NCC's strategic themes, quoted: verifiability requires backwards compatibility,
so *"the client increases in complexity over time"*; and documentation was
insufficient for **independent implementations** — *"For this to succeed,
independent implementations are needed, which in turn require thorough
documentation… some of the documents do not perfectly represent the current
state of Keybase's code."*

No public evidence found of a chain-rewrite incident or a real-world attack.
`[coverage: searched public writeups + the audit; did not read all 4,254 open
client issues]`

### (d) State after Zoom (acquired 2020-05-07) `[DEPLOYED, measured]`

Service is **alive**: keybase.io returns 200, the Merkle root is minutes fresh
(above), `keybase/client` still had commits on 2026-09-01. Community sentiment is
"abandonware" — reduced staffing, 4,254 open issues (measured via GitHub API
2026-09-02); the abandonment claim itself is *secondary reporting* (Arch forums,
a "KeyBase is DEAD" issue), not a company statement. No fork of the server has
been found; the client is open source, the server is not, so a fork of the
*transparency service* is not available. `[INFERENCE from measurement]`

### (e) Did the auditability ever get used? — the honest verdict

Evidence, all measured 2026-09-02 via the GitHub API:

- `keybase/blockchain` — the tool whose entire purpose is *"Read a user's Keybase
  sigchain out of the Bitcoin Blockchain"* — is **archived, last push
  2016-02-18, 7 stars**.
- The Keybase client itself, per a long-standing issue (`keybase/client#16673`),
  **does not read the blockchain**; blockchain checking was left to humans doing
  it manually.
- `google/keytransparency` (the industrial CONIKS descendant): **archived, last
  push 2021-07-05**, 1,569 stars.
- `coniks-sys/coniks-go`: last push 2022-10-06, 121 stars, never productionised.

**Verdict: no.** Keybase built a genuine, correct, continuously-running,
blockchain-anchored transparency log, and the anchor-verification tooling died
after ~20 months with 7 stars while the client never checked the anchor at all.
The one time the system's integrity was actually examined, it was a paid
$100k+ audit — *secondary reporting for the dollar figure; the audit itself is
primary* — not a user, not a monitor, not a third party. `[INFERENCE, but the
supporting measurements are direct]`

**This is the load-bearing finding for the island design.** The prior pass
concluded transparency failures are operational rather than adversarial; Keybase
adds that the *detection layer* is the part that goes unstaffed.

---

## 2. Has anyone shipped KT with no global root?

**Deployed: no. Specified: yes. Papers: yes.** Named coverage boundary at the end.

- **IETF KEYTRANS `draft-ietf-keytrans-architecture-05` §5.3, "Federation"**
  `[PAPER — active standards-track WG]`. Verbatim: *"In a federated application,
  many servers that are owned and operated by different entities will cooperate
  to provide a single end-to-end encrypted communication service"*; the end-user
  identity names the controlling entity; and crucially a server *"MAY act as an
  anonymizing proxy for its users when they query transparency logs run by other
  entities… but SHOULD NOT attempt to 'mirror' or combine other transparency
  logs with its own."* §5 adds *"Client implementations should generally be
  prepared to interact with multiple independent transparency logs."*
  **That is the per-island design, written down by the IETF.**
  The draft also states the gossip problem plainly: gossip *"is only secure if
  gossip can be implemented such that gossipping users are reasonably expected
  to form a connected graph of all users. If not, then the transparency log can
  attempt to partition users into subsets that do not gossip."*
- **CONIKS itself is per-provider, not global** `[PAPER]` — the brief's framing
  of CONIKS as centralised is only half right. Quoting the paper: *"Auditors…
  track the chain of signed 'snapshots' of the key directory. Auditors publish
  and gossip with other auditors to ensure global consistency. Indeed, CONIKS
  clients all serve as auditors for their own identity provider and providers
  audit each other."* The multi-provider, providers-audit-each-other half is the
  part that was **never deployed** — Google's descendant is archived, coniks-go
  is a research prototype.
- **ClaimChain** (Kulynych, Isaakidis, Troncoso, Halpin; arXiv 1707.06279)
  `[PAPER + prototype]` — per-user chains, *no global state at all*, cross-
  validated via gossiped cross-references; explicitly *"a middle-ground between…
  CONIKS and a fully decentralized but global state."* Prototype in Python; the
  paper notes it was *"currently being tested by the Autocrypt team"*. It never
  shipped in Delta Chat or Autocrypt as far as I can find.
- **EthIKS** (Bonneau, FC'16) `[PAPER]` — audits a CONIKS log via Ethereum. A
  global root by another name; not deployed.

**Refutation-shaped conclusion:** an absence of deployments here is *not* proof
the design is broken — the IETF endorses its shape — but it does mean **no
operational autopsy exists for it.** Every failure mode of a federated
no-global-root log is untested in the field. That is the honest risk statement.

---

## 3. ATProto / `did:plc`

- **Operator**: Bluesky Social PBC. `plc.directory` is live (`_health` → 200,
  2026-09-02). In Sept 2025 they announced transfer to an independent **Swiss
  Association** (atproto.com/blog/plc-directory-org). `[DEPLOYED]`
- **Trust model, from the PLC lead himself** — Daniel Holmgren, *"PLC Threat-
  modeling & Auditability"*, 2025-12-02: *"The directory cannot fake a valid
  operation, however the directory may reject a valid operation or remove a
  previously accepted operation."* The directory *"must be trusted with
  maintaining the set of accepted valid operations."* `[DEPLOYED, primary]`
- **On the global-root question, he lands where this project has landed**:
  *"We shouldn't consider either the sequence numbers of operations or the root
  hash of a tlog to be a 'global' value or something that divergent directories
  must reach consensus on… these should be thought of as mechanisms that allow
  for auditing a particular directory."* And on the alternative: *"I really
  don't think PLC should have a cryptocurrency, and I really don't want the UX
  hits of operating off a blockchain."* The answer to a lying directory is
  **credible exit**, not consensus. He concedes divergent directories *"will
  never have the same root hash for their tlog."* `[PAPER — stated intent]`
- **Auditing in practice — thin.** Microcosm, 2025-09-19: *"There might actually
  only be three public mirrors, all run custom code"*, *"Zero implement /export
  (for downstream mirrors) or operation validation (to accept writes)"*, *"Each
  is operated by just one individual."* `[DEPLOYED, one operator's inventory —
  secondary in the sense that I did not re-count the mirrors myself]`
- **Operational autopsy**: David Buchanan (retr0id), 2023-06-01 — DID truncation
  allowed "evil twin" DIDs sharing a genesis operation; he hijacked `@bsky.app`
  and states a PDS transfer would have given *"total control over the hijacked
  account."* Reported and patched the same day (17:03). His conclusion is the
  relevant one: *"we shouldn't have to rely on admins self-reporting these sorts
  of changes"* — he wanted CT-style independent monitoring, which did not exist.
- **`did:web`**: available, essentially unused — one atproto commentator puts it
  at *"99.99% of people don't use it"* `[secondary, an estimate not a
  measurement]`. Failure mode: it has **no history at all** — the current
  document is whatever the domain serves today, so it trades a trusted directory
  for a trusted DNS+TLS+webserver with zero auditability.
- **Governance documentation**: the atproto DID spec page is indeed silent. The
  substantive governance record is the `atproto.com/blog/plc-directory-org` post
  and Holmgren's leaflet — a blog and a personal post, **not the spec**.

---

## 4. Matrix — confirmed: no transparency log

- Matrix has cross-signing (MSC1756) and SAS verification. **MSC4153
  ("Exclude non-cross-signed devices")** doubles down on cross-signing and does
  *not* mention transparency logs; it concedes the underlying problem in passing
  — *"server admins can trivially create new devices for users"* — and then
  addresses it with device policy, not transparency. `[DEPLOYED spec work]`
- **The KT proposal is an ISSUE, not an MSC**: `matrix-org/matrix-spec#2075`
  "Transparency logs for public identity keys", filed **2025-02-21, still open,
  no assignee, no PR, no branch** (measured 2026-09-02). It cites WhatsApp's KT
  work, and asks precisely our question: *"Would you have one directory per
  homeserver, or is it better to aggregate them somehow — and if the latter, how
  does one do so securely?"*
- **So: no rejected MSC with reasoning exists.** The valuable artefact the brief
  hoped for is not there. What is there is weaker but still useful: the largest
  federated E2EE chat network looked at KT, could not decide between per-server
  and aggregate, and **has not started**. `[DEPLOYED absence]`

---

## 5. Nostr — no transparency, and no answer to key theft

- Key *is* identity; there is no rotation. If your key is stolen you make a new
  identity and rebuild your social graph out of band. `[DEPLOYED]`
- **NIP-41** exists only on branch `pf7z-nip41`, **not merged** into
  `nostr-protocol/nips` (measured 2026-09-02). Design: `kind:1776` pre-whitelists
  a backup pubkey, `kind:1777` announces migration, and **clients wait 60 days**
  before acting, counted from *first sighting*, not the event timestamp — an
  explicit admission that relays cannot be trusted for time and that the whole
  scheme is *"simple, best-effort, not guaranteed"*. It acknowledges a
  *"poorly-distributed evil kind:1777 attack"*.
- Competing drafts exist (a key-revocation NIP, a "lock the key" NIP where
  relays refuse events from blocked keys). None accepted.
- **The 60-day delay is the interesting artefact**: with no transparency log and
  no global ordering, the only defence left is *latency* — give the real owner
  time to shout. That is what a system with dumb relays and no log has to fall
  back on.

---

## 6. Everyone else — brief, only what bears

- **Delta Chat**: federated over SMTP, keys distributed in-band (Autocrypt).
  Encryption v2 (2025-08) pins keys to contacts. **No transparency log**;
  ClaimChain was the research attempt to give it one and did not land.
- **Threema / Wire**: centralised operators; QR/SAS verification, no KT log.
  Wire is MLS-based. **MLS deliberately punts** — the MLS architecture defines an
  "Authentication Service" as an abstract component and leaves KT to others.
  `[PAPER]`
- **Sigstore**: the closest *working* transparency culture (Rekor), but it is a
  **single global log with a global root** — the opposite of the per-island
  shape, and it works because CI systems verify automatically. `[DEPLOYED]`
  `[INFERENCE]` The distinguishing variable across everything here is not
  cryptography, it is **whether a machine checks the proof unattended**.
  Sigstore/CT: yes → used. Keybase/PLC: no → unused.
- XMPP/OMEMO, Briar, Mastodon: no KT work found worth reporting.

---

## What actually bears on the per-island design

1. **Shape is fine, staffing is the risk.** Per-island roots with no global root
   is IETF-blessed (§2). The unanswered question is not "is it sound" but "who
   ever looks", and every prior system answers *nobody*.
2. **Two islands is a degenerate gossip graph.** KEYTRANS's own condition —
   users must form a connected graph or the log can partition them — is
   *trivially* met at N=2 and *trivially* broken at N=3+ with no gossip
   protocol. Design the gossip before the log, or state that equivocation is
   undetected.
3. **Append-only + bugs = permanent scars.** Keybase's High finding came from
   its own server writing invalid links for 3.5 years into a record it could not
   edit. Any per-island log needs a *signed, chain-covered* exclusion mechanism
   from day one, not a client-side hardcoded list keyed on server-supplied data.
4. **Signature-payload versioning must be inside the signature.** NCC-KB2018-004
   produced equivocation *under a valid Merkle tree*. Whatever the island signs,
   the format version must be covered.
5. **Refutation to weigh**: nobody has ever operated the design. That is not the
   same as it being wrong, but it means the failure catalogue is empty, and the
   one system closest in spirit (PLC) has 3 mirrors, run by 3 individuals, none
   of which validate operations.

## Coverage boundary of this search

English-language web only; primary sources read directly for: the NCC PDF, the
CONIKS paper, the ClaimChain paper, the KEYTRANS architecture draft, Holmgren's
PLC post, Buchanan's writeup, NIP-41, MSC4153, matrix-spec#2075. Live measured:
keybase merkle root API, plc.directory health, four GitHub repos' archive state
and last-push. **Not searched**: non-English literature, closed enterprise
deployments, Matrix room logs / MSC discussion threads beyond the issue,
academic venues past a keyword sweep, and the 4,254 open Keybase client issues.
Absence claims above are bounded by exactly that.

## Sources

- https://keybase.io/docs-assets/blog/NCC_Group_Keybase_KB2018_Public_Report_2019-02-27_v1.3.pdf
- https://book.keybase.io/docs/server/merkle-root-in-bitcoin-blockchain · https://keybase.io/docs/sigchain
- https://github.com/keybase/client/issues/16673 · https://github.com/keybase/blockchain
- https://www.cs.wm.edu/~smherwig/readings/papers/15-sec-coniks.pdf · https://github.com/google/keytransparency
- https://arxiv.org/pdf/1707.06279 · https://jbonneau.com/doc/B16b-BITCOIN-ethiks.pdf
- https://www.ietf.org/archive/id/draft-ietf-keytrans-architecture-05.html
- https://dholms.leaflet.pub/3m6zswymcqk2p · https://atproto.com/blog/plc-directory-org
- https://updates.microcosm.blue/3lz7nwvh4zc2u · https://www.da.vidbuchanan.co.uk/blog/hacking-bluesky.html
- https://github.com/matrix-org/matrix-spec/issues/2075 · https://github.com/matrix-org/matrix-spec-proposals/blob/main/proposals/4153-invisible-crypto.md
- https://github.com/nostr-protocol/nips/blob/pf7z-nip41/41.md
