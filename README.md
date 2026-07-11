# aiko-chat-island

An **island** is a complete, self-contained
[`aiko`](https://github.com/geekscape/aiko_services) mesh — a **gateway** plus its
own broker, registrar, and [`ChatServer`](https://github.com/geekscape/aiko_chat) —
that federates with other islands as a peer, not as a client of any hub. The
island is the unit of federation. Two independent islands
(`chat.imagineering.cc`, `chat.enspyr.co`) currently run this code.

**This repo builds the gateway** — the one component that puts a stable WSS + REST
contract over the aiko MQTT backbone, so mobile/web clients get a durable HTTP/
WebSocket API instead of speaking MQTT. (The broker, registrar, and ChatServer
that complete an island run from the `aiko-bridge` image; see the compose file.)

> **Why the split?** aiko moves messages over MQTT with an actor / eventual-
> consistency model. Clients want a boring, durable API — auth, message history,
> read receipts, account deletion — none of which the bus provides. The gateway
> is the **sole bus participant on behalf of every user**: clients speak HTTP/WS
> to it; it speaks MQTT to the island. And islands federate by exposing their
> **gateways** to each other (the directory at `/v1/gateways` lists gateways, not
> brokers). That boundary is the whole design — see
> [Design 03](docs/design/03-auth-on-the-bus.html).

---

## Table of contents

- [What is an island](#what-is-an-island)
- [Architecture](#architecture)
- [Quick start (local dev)](#quick-start-local-dev)
- [Configuration](#configuration)
- [API surface](#api-surface)
- [Database & migrations](#database--migrations)
- [Deployment](#deployment)
- [Testing](#testing)
- [Design docs](#design-docs)
- [Repository layout](#repository-layout)

---

## What is an island

An **island** is a complete, independent aiko mesh: `gateway` + `mosquitto` +
`registrar` + `ChatServer`. Nothing external is required — no shared broker, no
matrix stack. Islands are peers, not clients of a hub; the gateway `directory`
(`/v1/gateways`) lets them discover each other so the app can offer a picker
instead of pasted URLs.

The whole stack ships in [`docker-compose.yml`](docker-compose.yml): the
`island` service is this repo; `registrar` and `chat` run from the
`aiko-bridge:latest` image with different `command:`s. Design goal held hard:
**one script, and it just works** — sane-secure defaults, the broker exposes no
host port.

## Architecture

```
  mobile / web client
        │  HTTP + WebSocket   (the stable contract)
        ▼
  ┌─────────────────────────────┐
  │  gateway (this repo)         │
  │  ├─ rest/*      FastAPI      │   auth, channels, messages, communities,
  │  ├─ realtime/*  WS hub       │   members, moderation, devices, gateways
  │  ├─ domain/*    services     │   the enforcement layer (one door per invariant)
  │  ├─ aiko/*      bus client   │   paho MQTT + ECConsumer(channel_list)
  │  └─ SQLite      local store  │   messages / auth / ACL — what HyperSpace can't hold
  └─────────────────────────────┘
        │  MQTT
        ▼
  mosquitto ── registrar ── ChatServer (owns channel creation; publishes channel_list)
```

The spine, in one sentence: the gateway **observes** channel topology off the
bus (an `ECConsumer` on ChatServer's `channel_list` share, drained through a
single ordered FIFO worker so add/remove pairs can't interleave), **persists**
messages into a local SQLite store, and **serves** the durable contract over
REST + WS. Bus threads hop onto the asyncio loop via
`call_soon_threadsafe`; the gateway suppresses its own echoes so a send isn't
persisted twice.

**Source of truth is split** (see [#1281](https://github.com/geekscape/aiko_services)):
HyperSpace/ChatServer is canonical for *channel existence*; the gateway's SQLite
is canonical for *data the bus cannot hold* — message history, auth, ACLs,
communities. Users are intended to become a HyperSpace `users` Category but that
is not yet populated upstream, so users live in SQLite today.

## Quick start (local dev)

Requires **Python 3.12** (aiko_services 0.6 caps at ≤3.13; we target 3.12).
`aiko_services` and `aiko_chat` are installed **editable from local checkouts**
(not on PyPI under a 3.12 pin) — clone them as siblings first.

```bash
# siblings: ../aiko_services and ../aiko_chat
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ../aiko_services -e ../aiko_chat
pip install -e '.[dev]'

cp .env.example .env          # sets ENVIRONMENT=dev (relaxes the prod fail-closed guards)
alembic upgrade head          # create the SQLite schema (./aiko_dev.db)
uvicorn aiko_gateway.main:app --reload --port 8095

curl localhost:8095/health    # {"status":"ok",...}
```

You can run the full island (broker + registrar + ChatServer) with
`docker compose up -d --build` instead — see [Deployment](#deployment).

> **Dev deliberately runs the same SQLite engine as prod**, so local work
> exercises SQLite's real behaviour (single-writer locking, type affinity, CHECK
> quirks, FK-off + application-level cascades) rather than being blind to it on
> Postgres.

## Configuration

All config is environment-driven via `pydantic-settings` ([`config.py`](src/aiko_gateway/config.py)).
The safety-critical default: **`ENVIRONMENT` defaults to `production`, which
fail-closes the boot** unless a strong (≥32-char, non-default) `JWT_SECRET` is
supplied. A non-prod `ENVIRONMENT` (`dev`/`test`/`local`) is what relaxes that.
Never deploy `.env.example` or anything derived from it.

| Group | Keys (selected) | Notes |
|---|---|---|
| Core | `ENVIRONMENT`, `JWT_SECRET`, `DB_URL` | prod requires a real secret; DB defaults to `./aiko_dev.db` |
| Bus | `AIKO_MQTT_HOST/_PORT`, `AIKO_NAMESPACE` | exported into `os.environ` for aiko_services |
| Registration | `OPEN_REGISTRATION` | explicit `true` is **rejected in prod** until membership I2 (#36) |
| Social sign-in | `SOCIAL_SIGNIN_ENABLED`, `APPLE_CLIENT_IDS`, `GOOGLE_CLIENT_IDS`, `SOCIAL_NONCE_REQUIRED` | Apple/Google ID-token verify + replay-nonce (#13) |
| OAuth broker | `GITHUB_CLIENT_ID/_SECRET`, `APP_OAUTH_CALLBACK_URL` | server-side code exchange + app-bound handoff (#21/#34) |
| Passkeys | `PASSKEY_ENABLED`, `PASSKEY_RP_ID`, `PASSKEY_IOS_APP_ID`, `PASSKEY_ANDROID_PACKAGE` | WebAuthn (#1471); domain-bound, advertised via `/v1/auth/providers` |
| Directory | `GATEWAY_BASE_URL/_ID/_DISPLAY_NAME`, `GATEWAY_SEED_PEERS`, `GATEWAY_GOSSIP_ENABLED` | island discovery; gossip fail-closed off (seed-peers suffice for 2 islands) |
| Guards | `RATE_LIMIT_ENABLED`, `AUTH_RATE_LIMIT`, `MAX_REQUEST_BYTES` | per-IP fixed-window + 64 KiB body cap on public ceremonies (#28) |

## API surface

FastAPI app (`aiko_gateway.main:app`), OpenAPI at `/docs`. Route groups:

| Router | Prefix | Purpose |
|---|---|---|
| `auth` | `/v1/auth`, `/v1` | register/login, social sign-in, OAuth broker, passkeys, `/me` |
| `channels` | `/v1` | channel list + history (mirrors bus topology) |
| `messages` | `/v1` | send + fetch messages |
| `members` | `/v1` | channel membership, roles, join policy |
| `communities` | `/v1` | nested-server discover / join / list (#32) |
| `moderation` | `/v1` | user blocks + message reports (Apple 1.2 / Google UGC) |
| `devices` | `/v1` | push-notification device-token registration (#16) |
| `gateways` | `/v1` | island directory (`/v1/gateways`) |
| `legal` | — | hosted `/privacy` and `/terms` |
| `well-known` | — | AASA + assetlinks for passkey domain binding |
| `ws` | — | the realtime WebSocket contract |
| — | `/health` | liveness + schema-at-head check |

## Database & migrations

**Alembic is the sole schema authority** ([#14](https://github.com/geekscape/aiko_services)).
`create_all` is demoted to test-only; the container entrypoint runs
`python -m aiko_gateway.migrate` (upgrade → head) **before** uvicorn, fail-closed
(`set -e`), so the app never serves an unmigrated schema. Adoption of a
pre-alembic prod DB stamps baseline `0001` only after `compare_metadata`
confirms zero drift.

```bash
alembic upgrade head                     # apply
alembic revision -m "describe change"    # new migration (hand-edit; SQLite is
                                         # blind to some ALTERs via batch_alter_table)
alembic history                          # must show a SINGLE head — two heads wedge boot
```

Migrations `0001`–`0010` cover: baseline, role/join_policy CHECK, device tokens,
OAuth handoffs + states, social nonces, app-challenge, passkeys, communities,
community default channel.

## Deployment

An island is **four containers from one published image** (gateway + broker +
registrar + ChatServer) plus stock `mosquitto`. CI builds that image multi-arch
(amd64 + arm64) and publishes it to `ghcr.io/nickmeinhold/aiko-chat-island` on
every push/tag — this is continuous **delivery** (an artifact), not deployment: an
island **pulls** it and never has CI push to it.

**Stand up a NEW island** — one script creates the volume, writes a production
`.env` with a generated JWT secret, pulls the image, starts the stack, and
terminates TLS:

```bash
./deploy/standup.sh --domain chat.example.org --name "Example Island"
```

**Update a running island** — backup → pull → recreate → verify:

```bash
./deploy/update.sh          # add --from-source to build from this checkout instead
```

See [`docs/standup-guide.md`](docs/standup-guide.md) for prerequisites, federation,
passkeys, build-from-source, and the manual step-by-step.

Load-bearing facts (still true, grounded against the live hosts):

- **Back up first** — `update.sh` does this fail-closed. The slim image has no
  `sqlite3` CLI; backups use Python's online `.backup()`
  (see [`docs/deploy-passkeys-runbook.md`](docs/deploy-passkeys-runbook.md)).
- The entrypoint migrates before serving; a failed migration fails the container.
- Pin a version with `ISLAND_VERSION` (default `edge` tracks `main`); `:latest`
  tracks the newest `v*` release tag.

> The two existing live islands (`chat.imagineering.cc`, `chat.enspyr.co`) still
> build-on-host from an rsync'd tree; cutting them over to pull-based updates is a
> one-time deploy, not yet done.

## Testing

```bash
pytest                # 32 test files; unit + route-table + wire e2e
```

The suite holds a **"never import `aiko_services`" isolation invariant** — the
production app is importable without the bus (the `AikoBusClient` import is lazy
inside `lifespan`), so tests can introspect the real route table and auth
dependency tree without standing up MQTT. FK-off + application-level cascades are
proven under an FK-*enforced* fixture (a behavioural probe, not just config).

## Design docs

Grounded HTML design docs (open in a browser):

- [01 — Channel topology reconcile](docs/design/01-channel-topology-reconcile.html)
- [02 — Bus decouple and islands](docs/design/02-bus-decouple-and-islands.html)
- [03 — Auth on the bus](docs/design/03-auth-on-the-bus.html) — where identity
  lives as the gateway becomes a true peer in a mesh of meshes.

## Repository layout

```
src/aiko_gateway/
  main.py         FastAPI app + lifespan (bus boot, ordered topology worker)
  config.py       pydantic-settings; fail-closed prod guards
  db.py           async engine (SQLite prod / aiosqlite tests), schema verify
  migrate.py      entrypoint migrate-to-head
  aiko/           bus client (paho), payload parsing, topology
  domain/         enforcement services (auth, channels, communities, moderation,
                  memberships, passkeys, oauth, nonce, rate_limit, …) + models
  realtime/       WebSocket hub + envelopes
  rest/           HTTP routers (see API surface)
alembic/versions/ 0001–0010 migrations
deploy/           mosquitto.conf
docs/             design docs + deploy runbook
tests/            unit + route + wire-e2e
```

---

Part of the aiko mesh: [`aiko_services`](https://github.com/geekscape/aiko_services)
(framework) · [`aiko_chat`](https://github.com/geekscape/aiko_chat) (ChatServer)
· `aiko_chat_app` (the mobile/web client).
