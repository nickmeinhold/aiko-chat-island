# Island vs Gateway — canonical taxonomy + wire contract

**Status:** island-owned decision, 2026-07-07 (claude-tasks#1760). Direction set by
Nick; confirm exact wording against the group thread if it shifts.

Two concepts the code has been conflating. They are 1:1 today but **not the same
thing** — collapsing them deletes the substrate-independence idea "gateway" names.

## The two terms

- **island** — the sovereign **node**. Identity, community, users, operator, data.
  The unit of *federation and choice*: you join / self-host / discover / vouch-for
  an island; sybil-resistance is priced per island. **The who/where.**
- **gateway** — the node's **protocol edge**. The substrate-agnostic seam that lets
  a client dial one Aiko-native API (`/v1/*` REST + WebSocket) without knowing what
  is behind it. The client speaks only `/v1/*` + WS — zero mosquitto/matrix — and
  the node translates that to its substrate. That translation *is* the gateway.
  **The how-you-reach.**

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
| Node identity / federation / choice | **island** | server picker, discovery directory, presets, the peer-entry *identity* (`id`, `display_name`) |
| Protocol edge / connection / transport | **gateway** | the `/v1/*` REST + WS API, `base_url`, the `aiko_gateway` package, `GatewayRestApi`/`gateway_transport` (app) |

## Island-repo internal naming (follow-up, non-breaking)

`peers_service.py` models a peer as `GatewayPeer` — but the entry *is a peer island*
(identity) reachable at a gateway `base_url`. An internal rename toward island-centric
names (the peer is an island; `base_url` is its gateway) would sharpen the split
without touching the wire. Tracked, not required for the app unblock.

## App-side (coordinated, after this lock)

Rename node-identity identifiers to island (`ServerEntry`→`IslandEntry`,
`kGatewayPresets`→`kIslandPresets`, picker, directory). **Leave** protocol-edge types
as gateway (`GatewayRestApi`, `gateway_transport`, `GatewayConfig`). Reading
`/v1/gateways` + `base_url` is unchanged. See app memory `concept_island_vs_gateway.md`.
