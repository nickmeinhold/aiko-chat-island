# CLAUDE.md — aiko-chat-island

Self-hosted gateway putting a stable WSS+REST contract over the aiko MQTT
backbone. One gateway + broker + registrar + ChatServer = an **island** (the
unit of federation). Start with [`README.md`](README.md) for architecture; this
file is the working-context that isn't obvious from the code.

## Gotchas that will bite you

- **CI is a live gate** (updated 2026-07-09 — the old "Actions out of minutes,
  permanent" is FALSE; that stale belief nearly buried a real bug). Two GitHub
  Actions workflows run on every PR and push to `main`: `CI`
  (`.github/workflows/ci.yml`, the `pytest` suite on 3.12) and `bus-e2e`
  (round-trip). Don't merge over a red check. STILL verify LOCALLY first (`pytest`)
  and report that as your gate — but green-locally ≠ green-on-CI: a fresh
  low-uptime CI runner surfaced a `time.monotonic()` JWKS bug that passed on
  high-uptime dev boxes (#64). Local pytest is the habit; CI is the confirmation.
- **Deploy is PULL-BASED over ssh** (since the 2026-07-11 standup pipeline; the
  old "`docker compose up -d --build`" model is DEAD for the islands). The live
  islands run a published, version-pinned image
  (`ghcr.io/nickmeinhold/aiko-chat-island:${ISLAND_VERSION}`, pinned in each box's
  `.env`) — there is NO `build:` on the box, so `--build` is wrong here. Ship a fix:
  merge → cut a `vX.Y.Z` tag (CI `release.yml` publishes the multi-arch image; `edge`
  tracks `main`) → bump the box's `ISLAND_VERSION` pin → `deploy/update.sh`
  (backup-first fail-closed → `compose pull && up -d` → verify `/health`). Verify the
  RUNNING container's image ref, not this doc, for the live mechanism. Slim image has
  no `sqlite3` CLI → Python `.backup()` (`update.sh` does it). See
  `docs/deploy-passkeys-runbook.md`. Two live islands: `chat.imagineering.cc`
  (`~/apps/aiko-chat-gateway`), `chat.enspyr.co` (`~/apps/aiko-chat-gateway`, `ssh
  nick-mel`, `sudo -n docker`). NOTE: imagineering's separate `matrix-*` stack is
  still the old build-on-host model — don't conflate it with the island.
- **Alembic is the sole schema authority.** `alembic history` must show a SINGLE
  head — two `0006`s merge clean in git but wedge boot (mergeable ≠ correct).
  SQLite is blind to some ALTERs; hand-edit migrations via `batch_alter_table`.
- **Dev runs SQLite on purpose** (matches prod). Prod deliberately runs FK-off
  with application-level cascades — do NOT "fix" that to FK-on (it's anti-parity;
  #1544 tracks the create-path work that would be needed first).
- **Test isolation invariant:** the suite must be able to `import aiko_gateway.main`
  WITHOUT `aiko_services` present (the `AikoBusClient` import is lazy inside
  `lifespan`). Don't hoist bus imports to module scope — it breaks clean-checkout
  route-table tests.
- `main.py`'s header docstring is **stale** ("persist into Postgres / Phase 1 /
  auth lands next") — reality is far past it. Trust the code, not that docstring.

## Working conventions

- **Conventional Commits.** Branch off `main`; commit + push proactively.
- **Trust boundaries are cage-match by law.** Auth, moderation, communities,
  account-deletion, wire-format, and state-lifecycle changes get an adversarial
  different-family review (`/cage-match`), not solo self-review. Doc-only and
  single-file trivia can self-review.
- **Enforce at the backend, through one door.** Seal the shared mutator (the
  service), not each caller — so route, in-process, and test paths all pass the
  same gate. Fold visibility predicates INTO the write (atomic), don't
  observe-then-write (TOCTOU).
- **Ground upstream claims against source, not authority.** Andy Gelme owns
  `aiko_services`/`aiko_chat`; his answers are gold but verify against the actual
  checkout (`../aiko_chat`) before building on them — delight is the signal to
  verify, not permission to assert.

## Where things live

- Design docs: `docs/design/0{1,2,3}-*.html` (topology, bus-decouple, auth-on-bus).
- Memory dir (this project's recall):
  `~/.claude/projects/-Users-nick-git-orgs-aiko-aiko-chat-island/memory/`.
- Tasks: `nickmeinhold/claude-tasks`, label `project:aiko-chat-island`.
- Sibling repos (editable installs): `../aiko_services`, `../aiko_chat`; the
  client is `aiko_chat_app`.
