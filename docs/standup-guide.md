# Stand up your own island

An **island** is a complete, self-contained [`aiko`](https://github.com/geekscape/aiko_services)
chat mesh — a gateway (this repo) plus its own broker, registrar, and ChatServer —
that federates with other islands as a **peer, not a client of any hub**. Running
your own island makes you a sovereign node in the federation. This guide gets you
from a bare host to a live, HTTPS-reachable island with **one script**.

> Already know the drill? Jump to the [TL;DR](#tldr). Want to understand what the
> script does before running it? Read [What the script does](#what-the-script-does).

## Table of contents

- [What you need first](#what-you-need-first)
- [TL;DR](#tldr)
- [What the script does](#what-the-script-does)
- [Federating with other islands](#federating-with-other-islands)
- [Turning on passkeys](#turning-on-passkeys)
- [Backups (do this before real users)](#backups)
- [Re-running, upgrading, tearing down](#re-running-upgrading-tearing-down)
- [Manual standup (no script)](#manual-standup-no-script)
- [Troubleshooting](#troubleshooting)

---

## What you need first

Three things, none of them exotic:

1. **A Linux host with a public IP** and **Docker** (Engine + the `docker compose`
   v2 plugin). A small cloud VM is plenty. You need ports **80 and 443** reachable
   from the internet (for TLS) — check your cloud security group *and* any on-box
   firewall (some images ship a default-REJECT `iptables`).
2. **A domain** (or subdomain) you control, with a DNS **A record pointing at the
   host** — e.g. `chat.example.org → 203.0.113.7`. This is not optional: passkeys
   are domain-bound and TLS certificates are issued per-hostname. Set the record
   *before* running the script so the certificate can issue on first boot.
3. **This repo, checked out on the host.** `git clone` it wherever you keep apps.

The host tools the script needs (`docker`, `git`, `openssl`, `curl`) are checked
in a preflight step — it fails early and tells you what's missing.

---

## TL;DR

```bash
# on the host, in the aiko-chat-island checkout:
./deploy/standup.sh --domain chat.example.org --name "Example Island"
```

That's the whole thing. In a few minutes you'll have:

- a persistent **data volume** created,
- a production **`.env`** written with a freshly-generated strong JWT secret,
- the **official island image pulled** (multi-arch) and the **gateway + broker +
  registrar + ChatServer** running — four containers from one image plus stock
  mosquitto, nothing built on the host,
- **Caddy** terminating TLS with an auto-provisioned Let's Encrypt certificate,

and the script will print `https://chat.example.org/health is live 🎉` once it has
verified the whole path end-to-end.

To also list an existing island so the app's picker shows both:

```bash
./deploy/standup.sh --domain chat.example.org --name "Example Island" \
  --seed-peers '[{"id":"chat.imagineering.cc","display_name":"Aiko","base_url":"https://chat.imagineering.cc"}]'
```

Run `./deploy/standup.sh --help` for every flag.

---

## What the script does

An island is **four containers from one image** — the gateway, and its own broker,
registrar, and ChatServer — plus stock `mosquitto`. The gateway image
(`ghcr.io/nickmeinhold/aiko-chat-island`, published multi-arch by CI) already
bundles `aiko_services` + `aiko_chat`, so it serves all three aiko roles by
`command:` override. The script does nothing magic; every step is one you could run
by hand (see [Manual standup](#manual-standup-no-script)).

| Step | What | Why it's needed |
|---|---|---|
| Preflight | check tools + DNS | fail early, not halfway through |
| 1 | `docker volume create aiko_data` | the SQLite store's home is declared `external` in compose (decoupled from the project name so a rename never empties it) — external volumes must exist before `up` |
| 2 | write `.env` | island identity (domain, display name, peers) + a **generated ≥32-char JWT secret**. `ENVIRONMENT` is left unset on purpose — absence means *production*, which fail-closes the boot unless a strong secret is present |
| 3 | `docker compose pull && up -d` | pulls the official image and starts all four containers. No `--build` — nothing is built on the host. (Want to build from your own checkout instead? `--from-source`.) The container migrates the DB to head, then serves |
| 4 | bring up Caddy | TLS termination + reverse proxy to `127.0.0.1:8095`, in its own stack so it never collides with an existing proxy |

Two safety properties worth knowing:

- **It's idempotent.** Re-running skips volume creation if it already exists, and it
  **never rotates an existing JWT secret** (that would sign out every user). Safe to
  re-run to change the display name or add peers.
- **The secret never leaves the host.** `.env` is written mode `600` and is
  gitignored. There is no phone-home.

---

## Federating with other islands

Federation is peer-to-peer: there is no central registry. Each island advertises a
small **operator-curated peer list** — full entries you put there by hand, merged
with no network fetch, so they're authentic by construction. The app's island
picker (`GET /v1/islands`) reads this to offer a menu instead of pasted URLs.

To list peers, pass `--seed-peers` a JSON array of `{"id","display_name","base_url"}`:

```bash
./deploy/standup.sh --domain chat.example.org --name "Example Island" \
  --seed-peers '[
    {"id":"chat.imagineering.cc","display_name":"Aiko","base_url":"https://chat.imagineering.cc"},
    {"id":"chat.enspyr.co","display_name":"Enspyr","base_url":"https://chat.enspyr.co"}
  ]'
```

Federation is **mutual by convention**: for the other islands to list *you*, their
operators add your entry to *their* seed peers and redeploy. Send them your
`{"id","display_name","base_url"}` (id is conventionally your domain).

> Gossip-based auto-discovery exists in the code but ships **off** (`GATEWAY_GOSSIP_ENABLED=false`)
> — for a handful of islands, seed peers are simpler and avoid the fetch/SSRF
> surface. You don't need it.

---

## Turning on passkeys

Passkeys (WebAuthn) are the app's **primary sign-in**, but they're **domain-bound**:
the browser/OS will only complete the ceremony if your island serves valid
`/.well-known` association files for *your* domain. So the script leaves passkeys
**off by default** — advertising them before the well-known files are correct makes
the app start a sign-in that dies mid-ceremony.

The gateway already **serves** these files (routes exist); what makes them valid is
your app's identifiers. Two files, both served by the gateway at your domain:

- `GET /.well-known/apple-app-site-association` — iOS `webcredentials`, keyed by the
  app's Team ID + bundle ID.
- `GET /.well-known/assetlinks.json` — Android Digital Asset Links, keyed by the
  app's package name + its **Play App Signing** SHA-256 fingerprint (the first one
  matters: Google re-signs the AAB, so a real store install runs *that* key).

These identifiers are baked as compose defaults for the reference Aiko app. If you
ship your **own** app build, override `PASSKEY_IOS_APP_ID` / `PASSKEY_ANDROID_PACKAGE`
/ `PASSKEY_ANDROID_CERT_SHA256` in `.env` first. Then verify the files serve, and
only then advertise:

```bash
# 1. confirm the well-known files serve for YOUR domain:
curl -s https://chat.example.org/.well-known/apple-app-site-association | jq
curl -s https://chat.example.org/.well-known/assetlinks.json | jq   # non-empty array

# 2. advertise passkey sign-in:
./deploy/standup.sh --domain chat.example.org --name "Example Island" --enable-passkeys
# (equivalently: set PASSKEY_ENABLED=true in .env and `docker compose up -d --build`)

# 3. confirm it's advertised:
curl -s https://chat.example.org/v1/auth/providers | jq   # includes {"slug":"passkey"}
```

Passkeys are only truly **live** once a real device completes a register→authenticate
round-trip. See [`deploy-passkeys-runbook.md`](deploy-passkeys-runbook.md) for the
device end-to-end procedure and the Android Play-signing details.

---

## Backups

The island's SQLite store (message history, auth, ACLs, communities) is the **only
copy** of that data, living in the `aiko_data` volume. **Set up a backup before real
users arrive.** The container is slim and has no `sqlite3` CLI, so use Python's
online `.backup()` — the exact hot-copy procedure (and a project-cutover checklist)
is in [`deploy-passkeys-runbook.md`](deploy-passkeys-runbook.md) § "Back up the
sole-copy prod DB".

---

## Re-running, upgrading, tearing down

```bash
# UPDATE to the latest published image — backup -> pull -> recreate -> verify:
./deploy/update.sh                              # the safe path (fail-closed on backup)

# change display name / add peers / flip a flag — safe, keeps data + secret:
./deploy/standup.sh --domain chat.example.org --name "New Name" --seed-peers '[…]'

# pin a specific version instead of tracking `edge` (main):
ISLAND_VERSION=v0.1.0 ./deploy/update.sh        # or set ISLAND_VERSION in .env

# stop the island (data volume survives — `down` without -v keeps volumes):
docker compose down
docker compose -f deploy/caddy/docker-compose.caddy.yml down

# nuke everything INCLUDING data (irreversible — back up first):
docker compose down && docker volume rm aiko_data
```

`update.sh` is the recommended update path: it hot-copies the sole-copy SQLite
store to `backups/` **before** touching the stack (aborting if the backup doesn't
land), then `docker compose pull && up -d`, then verifies `/health`. Add
`--from-source` to build from your checkout instead of pulling.

---

## Manual standup (no script)

If you'd rather run the steps yourself (or debug one), this is exactly what the
script automates:

```bash
# 1. create the external data volume
docker volume create aiko_data

# 2. write .env (production: NO ENVIRONMENT line; strong secret required)
cat > .env <<EOF
JWT_SECRET=$(openssl rand -hex 32)
GATEWAY_BASE_URL=https://chat.example.org
GATEWAY_DISPLAY_NAME=Example Island
PASSKEY_RP_ID=chat.example.org
GATEWAY_SEED_PEERS=[]
PASSKEY_ENABLED=false
EOF
chmod 600 .env

# 3. pull the official image + bring up the island (migrates then serves).
#    All four containers run from this one image (bar stock mosquitto).
docker compose pull && docker compose up -d
curl -s http://127.0.0.1:8095/health         # {"status":"ok",...}
#    (build from THIS checkout instead of pulling:)
#    docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build

# 4. TLS
echo "ISLAND_DOMAIN=chat.example.org" > deploy/caddy/.env
docker compose -f deploy/caddy/docker-compose.caddy.yml up -d
curl -s https://chat.example.org/health      # via Caddy + Let's Encrypt
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `JWT_SECRET required` at boot | `.env` missing or secret too short | the script generates one; if hand-writing, use `openssl rand -hex 32` |
| Container restarts / `/health` never answers | migration failed, or bad config | `docker compose logs chat-island` — the entrypoint migrates before serving and fails closed |
| HTTPS never comes up | DNS not pointing here yet, or 80/443 blocked | confirm `getent hosts chat.example.org` = this host's IP; open 80+443 in the cloud SG **and** on-box firewall; `docker compose -f deploy/caddy/docker-compose.caddy.yml logs` |
| image pull fails / `manifest unknown` | wrong/absent `ISLAND_VERSION` tag | default is `edge`; pin a real tag (`ISLAND_VERSION=v0.1.0`) or build locally with `--from-source` |
| App picker doesn't show my island | seed peers not mutual | the *other* island's operator must add your entry too |
| Passkey sign-in starts then dies | advertised before well-known files valid | set `PASSKEY_ENABLED=false`, fix `/.well-known`, re-verify, re-enable |

### FAQ

**I already run mosquitto / Postgres / Redis / something on this host — will it clash?**
No. The island is **hermetic**: its broker publishes no host port, the gateway
binds only `127.0.0.1:8095`, and its store is a named Docker volume. It never
reaches for a host resource, so there's nothing to collide with. Your existing
services are untouched, and the island brings its own broker invisibly. (Reusing
*your* broker isn't supported on purpose — the island's isolated broker is what
keeps it an independent, self-contained node.)

**I already run Caddy / nginx / Traefik on this host — do I have to use the bundled Caddy?**
No — run the script with `--no-tls` and point your existing proxy at
`127.0.0.1:8095` (add a vhost / `reverse_proxy`). The reverse proxy is the one
piece it makes sense to reuse; it lives *outside* the sealed island for exactly
that reason. Everything else (gateway, broker, registrar, ChatServer) is the
island's own and isn't shared.

For the deep operational details (deploy invariants, DB backup/restore, passkey
device e2e), see [`deploy-passkeys-runbook.md`](deploy-passkeys-runbook.md) and the
[README](../README.md).
