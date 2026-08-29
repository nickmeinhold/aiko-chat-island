# ISL-0003: Deploy by pulling a version-pinned published image

| | |
|---|---|
| **ADR** | 0003 (island) |
| **Status** | Accepted (retroactive) |
| **Owner** | Nick Meinhold |
| **Created** | 2026-08-23 (documenting the 2026-07-11 standup pipeline) |
| **Thread** | — |

## Summary

An island runs `ghcr.io/nickmeinhold/aiko-chat-island:${ISLAND_VERSION}`, pinned
in its own host `.env`. There is **no `build:` key in `docker-compose.yml`** and no
compiler on the box. CI publishes a multi-arch image; each island pulls it on its
own schedule. **CI holds an artifact, never production credentials.**

## Motivation

Islands are sovereign nodes run by different people on different hardware (both
live boxes are arm64; third-party operators are often amd64). Three properties
follow from that and none of them survive building on the host:

- **An operator must be able to run a known version**, not "whatever `main` was
  when I last pulled". A pinned tag is a fact you can state in an incident.
- **A build needs a toolchain, and a toolchain is an attack surface and a
  maintenance burden** on a box whose owner signed up to run a chat server.
- **Nobody should have to hand their production SSH credentials to someone else's
  CI** in order to receive an update.

One image serves every role — gateway (default), `aiko_registrar`, and
`chat_start.sh` — by `command:` override, because it already bundles
`aiko_services` and `aiko_chat`. The only other container is stock
`eclipse-mosquitto`.

## Proposal

Shipping a fix is four steps, in order:

1. Merge to `main`.
2. Cut a `vX.Y.Z` tag. `release.yml` builds both architectures **natively** (public
   repos get free arm64 hosted runners, so no QEMU emulation of a crypto-heavy
   image), pushes each by digest, and stitches them into one multi-arch manifest.
   `edge` tracks `main`; `latest` follows semver only.
3. Bump `ISLAND_VERSION` in the box's `.env`.
4. Run `deploy/update.sh` — **backup-first and fail-closed**: it takes an online
   hot copy of the SQLite store and aborts before touching anything if the backup
   does not land, then `compose pull && up -d`, then verifies `/health`.

`--build` is meaningless here. The slim image has no `sqlite3` CLI, so the backup
uses Python's `.backup()` inside the container.

## Rationale and alternatives

- **Why not push-CD (CI deploys to the boxes)?** It puts production SSH
  credentials in CI, which inverts the sovereignty the whole design rests on: an
  operator would be accepting remote code execution from a repository they do not
  control. Pull-based means the island decides *when*, and CI never holds anything
  it could use against a box.
- **Why not Watchtower?** It does its own pull-and-recreate, bypassing the
  backup-first and health-verify steps that make `update.sh` safe. It also has no
  rollback, and it was archived by its owner in December 2024.
- **Why not build on the box (the pre-2026-07-11 model)?** It made every island's
  running code a function of its local checkout, so "what version are you on?" had
  no answer. Note imagineering's *separate* `matrix-*` stack is still on that
  model — do not conflate it with the island.

## Unresolved questions

**This deploy is manual, and automating it is blocked for a good reason.** A full
`/crucible` on reactive deploy converged on a **FATAL** at cross-family review:
image rollback is not database rollback. A release carrying a migration takes a
backup at schema *N*, migrates the persistent volume to *N+1*, fails `/health`,
and rolls back to the old image — which now runs against a forward-migrated
schema. Crash-loop or corruption. As Tesla put it: *"backup without
restore-on-failure is a souvenir, not a spine."*

Operator-opt-in auto-pull (default off) is the re-picked direction, gated on the
additive-only migration lint from ISL-0001 (#3188 / #2615). See
`docs/crucible/reactive-deploy/`.

## Consequences, learned the hard way (2026-08-23)

**`update.sh` pulls the image; it does not sync `docker-compose.yml`.** The compose
file on a box is a *separate artifact* from the one in this repo, and it drifts
silently. At the v0.7.0 deploy both live islands were found running a compose file
that matched neither `v0.6.0` nor `v0.7.0` — each missing environment forwards
added months earlier. Every health check was green about it.

So a release whose payload is a compose change (new `environment:` entries, for
instance) is **not delivered by pulling the image**. Until that is enforced
mechanically (#2301), treat the box's compose as something to verify, not assume.
