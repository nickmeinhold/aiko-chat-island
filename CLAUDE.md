# CLAUDE.md — aiko-chat-island

Self-hosted gateway putting a stable WSS+REST contract over the aiko MQTT
backbone. One gateway + broker + registrar + ChatServer = an **island** (the
unit of federation). Start with [`README.md`](README.md) for architecture; this
file is the working-context that isn't obvious from the code.

## Gotchas that will bite you

- **CI is unavailable** (GitHub Actions out of minutes, permanent). The
  verification burden is LOCAL — run `pytest` and report *that* as the gate.
  Never say "CI will catch it." Admin-merge over the absent Actions check is
  expected.
- **Deploy is manual `docker compose up -d --build` over ssh** — no pipeline.
  `--build` is mandatory (image is `build: .`, no registry); without it you ship
  the stale image and exit 0. Back up the DB first (slim image has no `sqlite3`
  CLI → use Python `.backup()`; see `docs/deploy-passkeys-runbook.md`). Two live
  islands: `chat.imagineering.cc`, `chat.enspyr.co`.
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
