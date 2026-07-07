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

## Wire contract decision: NO breaking change

`GET /v1/gateways` → `{"gateways": [{"id", "display_name", "base_url"}, ...]}`

- **Keep the path `/v1/gateways`.** Each entry is a peer **island**, *addressed by
  its gateway's URL* — the name reads correctly under the taxonomy. Renaming is
  breaking (shipped app builds call it) for zero semantic gain.
- **Keep the keys `{id, display_name, base_url}`.** `base_url` correctly names the
  **gateway** edge; `id` + `display_name` are the **island's** identity. No rename.
- **Semantics (documented, not encoded):** one entry = one peer island; `base_url`
  = that island's gateway (protocol-edge) URL.

The split is therefore **conceptual + spoken + internal-identifier only** — it costs
no wire migration. The app can rename its node-identity identifiers freely while
still reading `/v1/gateways` + `base_url` unchanged.

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
