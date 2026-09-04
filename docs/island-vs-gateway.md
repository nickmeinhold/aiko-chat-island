# Island vs Gateway — canonical taxonomy + wire contract

**Status:** island-owned decision, 2026-07-07 (claude-tasks#1760). Direction set by
Nick; confirm exact wording against the group thread if it shifts.

Three concepts the code has been conflating. The first two are 1:1 today but **not
the same thing** — collapsing them deletes the substrate-independence idea "gateway"
names. The third was missing from this document until 2026-09-02, and its absence
caused real mis-renames (see "The third term", below).

## The three terms

- **island** — the sovereign **node**. Identity, community, users, operator, data.
  The unit of *federation and choice*: you join / self-host / discover / vouch-for
  an island; sybil-resistance is priced per island. **The who/where.**
- **gateway** — the node's **protocol edge**. The substrate-agnostic seam that lets
  a client dial one Aiko-native API (`/v1/*` REST + WebSocket) without knowing what
  is behind it. The client speaks only `/v1/*` + WS — zero mosquitto/matrix — and
  the node translates that to its substrate. That translation *is* the gateway.
  **The how-you-reach.**
- **server** — the **host** the island runs on. A box, a VM, a process's clock, a
  disk, an uptime. Nick, 2026-09-02: *"an island does live on a server, so we could
  still easily have references to 'server' that should have survived."* **The
  what-it-runs-on.**

### The third term, and why omitting it was expensive

This document said "the two terms" from 2026-07-07 until 2026-09-02, and both repos
implemented it faithfully. So every identifier naming a HOST or PROCESS property had
to be forced into one of two bins that did not fit, by feel — and the app's
vocabulary sweep provably split on identical cases:

| Original | Became | Verdict |
|---|---|---|
| `closeFromServer` | `closeFromGateway` | correct — a WS close frame comes from the bridge service |
| `_parseServerTime` | `_parseIslandTime` | **wrong** — same category, same sweep, other bin (fixed: app PR#180) |

Neither a guard nor a five-family adversarial review could catch that: both were
handed this two-bin taxonomy as a premise. A review interrogates whether things are
sorted correctly; it does not ask whether the bins are right. The correction came
from outside the work entirely.

**The rule.** Ask what the identifier NAMES. Identity / federation / choice →
*island*. Protocol edge / connection / transport → *gateway*. Host / box / clock /
disk / process → *server*, which is a LEGITIMATE word for that meaning; it is banned
only where it would mean one of the first two. A name that resists all three bins is
a finding — say so rather than picking the nearest.


An **island HAS a gateway** (the landmass vs its front door). This repo is named
for the node (`aiko-chat-island`) and *contains* the gateway implementation (the
`aiko_gateway` package) — that nesting is correct, not a naming bug.

## Wire contract decision: migrate to `/v1/islands`, deprecate `/v1/gateways`

The node directory is a directory of **islands** — carry the taxonomy through to the
wire, with a compat window so shipped app builds and pre-taxonomy peers don't break.

- **Canonical: `GET /v1/islands`** → `{"islands": [{"id", "display_name", "base_url"}, ...]}`.
  Each entry is a peer **island** (`id`/`display_name` = identity); `base_url` is
  that island's **gateway** edge. The array key is `islands`.
- **Deprecated alias: `GET /v1/gateways`** → `{"gateways": [...]}` (same entries,
  legacy envelope key). Kept for the compat window so shipped app builds and peers
  still on the old build keep working. Remove once the app has adopted `/v1/islands`
  and old builds have aged out (coordinate via #1760).

  **STATUS 2026-09-02: the app has adopted `/v1/islands` and dropped its `gateways`
  compat entirely** (app PR#177, Nick's call: *"we have the only two islands, we
  don't need to keep any references to gateways"*). The alias now serves only peer
  gossip from a node on an old build. Since we operate every island in existence the
  removal condition is effectively met — but removing it is a WIRE change, so it gets
  its own decision rather than riding this one.
- **Keys unchanged** `{id, display_name, base_url}` — no per-entry key churn; only the
  collection name moves. `base_url` still names the gateway edge.
- **Gossip converges both directions during rollout:** a node fetches `/v1/islands`
  first and falls back to `/v1/gateways`, parsing either envelope key
  (`peers_service.gossip_once`). So new↔old node pairs still converge.

*(Supersedes the initial "no breaking change / keep `/v1/gateways`" call — Nick's
direction was to finish the taxonomy on the wire, which the compat window makes
safe. Implemented 2026-07-08.)*

## Which names go which way

| Surface | Term | Examples |
|---|---|---|
| Node identity / federation / choice | **island** | island picker, discovery directory, presets, the peer-entry *identity* (`id`, `display_name`) |
| Protocol edge / connection / transport | **gateway** | the `/v1/*` REST + WS API, the `aiko_gateway` package, `GatewayRestApi`/`gateway_transport` (app) |
| A base URL used as **self-description** — "what is my own address" | **gateway** | island's `GATEWAY_BASE_URL` (derives the OAuth `redirect_uri`, validates `PASSKEY_RP_ID`, and is what this node advertises about itself in the directory) |
| A base URL used as **selection** — "which island am I pointing at" | **island** | app's `ISLAND_BASE_URL` dart-define (seeds the initial pick; the user re-points it in-app) |

### `base_url` is TWO concepts that share a value — this table used to collapse them

Until 2026-09-04 the gateway row simply listed ``base_url``, and that single row manufactured
a cross-repo disagreement that was not real. The app renamed its dart-define
``GATEWAY_BASE_URL`` → ``ISLAND_BASE_URL`` (app PR#178) while the island kept
``GATEWAY_BASE_URL`` (island PR#156, applying the old row) — and both were CORRECT, because
they name different jobs that happen to hold the same string:

- **Self-description** is gateway-shaped. The island publishes an address ABOUT ITSELF and
  derives its own redirect from it. That is the protocol edge describing where it answers.
- **Selection** is island-shaped. Choosing that URL in the app IS choosing which sovereign
  deployment you belong to — "the unit of federation and choice", which is this document's
  own definition of an island.

**Neither repo moves code. Nothing on the wire changes under any reading** — the directory
payload's per-entry ``base_url`` field is untouched, and remains that island's gateway edge.

**The general lesson, since this doc caused it twice** (the two-bin taxonomy that omitted
`server`, and now this): a row that lists a VALUE rather than a JOB will eventually be applied
to two different jobs that share that value, and the resulting disagreement will look like
drift between the people applying it. Name the job, not the field.

## Island-repo internal naming (DONE)

`peers_service.py` now models a node as `Island` (was `GatewayPeer`) in an
`IslandDirectory` (was `PeerDirectory`); `coerce_island` (was `coerce_peer`). The
entry *is a peer island* (its `id`/`display_name`); `base_url` is that island's
gateway edge. Wire untouched by the rename (keys unchanged). **Deferred, deploy-coupled:**
the self-identity config env vars are still `GATEWAY_*` (`gateway_base_url`,
`gateway_display_name`, `GATEWAY_SEED_PEERS`).

**DONE 2026-09-02.** Three env vars named the island, not the gateway edge, and moved:

| Was | Now | Why |
|---|---|---|
| `GATEWAY_ID` | `ISLAND_ID` | the island's identity in the directory |
| `GATEWAY_DISPLAY_NAME` | `ISLAND_DISPLAY_NAME` | the island's label |
| `GATEWAY_SEED_PEERS` | `ISLAND_SEED_PEERS` | full peer-ISLAND entries |

**`GATEWAY_BASE_URL` deliberately KEEPS its name.** This section previously listed it
with the others while the table above said "`base_url` still names the gateway edge" —
a contradiction, resolved in favour of the table: a base_url IS the edge address. Also
kept: `GATEWAY_BOOTSTRAP_PEERS` (peer *edges* — bare base URLs, not island identities)
and `GATEWAY_GOSSIP_ENABLED` / `_INTERVAL_SECONDS` (they configure the gateway's
polling behaviour, not who the island is).

**It was never actually deploy-coupled**, which is why it sat deferred for two months
for no reason. Both names are real `Settings` fields; `_adopt_legacy_gateway_identity`
takes the canonical one only when it is non-blank, so a box on the old `.env` keeps
working untouched.

**The obvious implementation is wrong, and silently.** `AliasChoices("ISLAND_ID",
"GATEWAY_ID")` fails here: compose forwards an unset var as the EMPTY STRING, and
AliasChoices takes the first key PRESENT — so `ISLAND_ID: ${ISLAND_ID:-}` always wins
and is always empty, and both live islands would have booted with no identity.
Measured before the resolver was written. `resolve-gateway-env.sh` reads both names
for the same reason: reading only the canonical one would empty the federation link
on the next standup re-run, which is #3734 re-opened by a rename.

**Cutover order:** deploy the image FIRST (it understands both), then flip each box's
`.env`, then delete the legacy fields, their resolver, the compose forwards, the
legacy rung in `resolve-gateway-env.sh`, and the two legacy tests.

## App-side (coordinated, after this lock)

Rename node-identity identifiers to island (`ServerEntry`→`IslandEntry`,
`kGatewayPresets`→`kIslandPresets`, picker, directory). **Leave** protocol-edge types
as gateway (`GatewayRestApi`, `gateway_transport`, `GatewayConfig`). Adopt the
canonical `GET /v1/islands` (read the `islands` array); `/v1/gateways` remains a
deprecated alias through the compat window, so old builds keep working until they age
out. `base_url` per entry is unchanged. See app memory `concept_island_vs_gateway.md`.
