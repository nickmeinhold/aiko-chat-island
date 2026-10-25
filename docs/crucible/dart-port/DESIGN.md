# Design: port the island gateway to Dart, against recordings, and switch once

*Movement 4 (Cast), second pass. 2026-10-25. Bundle: `CRUCIBLE.md` (the case), `RESEARCH.md` (Heat), `SPARK.md` (the sparks and their fusion). The first cast followed the fusion literally; what the second cast changed, and why, is in §9.*

## 0. Blast radius and consent, first

- **Who this reaches.** Every user of both islands, at one moment per island: the switch.
  - The riskiest surface is sign-in. A Dart WebAuthn verifier that rejects a valid passkey locks people out; one that accepts an invalid one lets someone in as someone else.
  - The second riskiest is message history, because the Dart process writes into the same SQLite file Python built.
- **Owners.**
  - Nick decides each switch, per island.
  - Andy is asked before `aiko_services_dart` becomes a production dependency, because ADR-0007 is his and the library has no licence.
  - The app tab is told before the switch, but nothing on the wire changes (§2.6).
- **Throttle.**
  - Nothing runs in production until the very last steps (§3, steps 8 and 9).
  - syd (the test island) switches first, then melb.
  - Rollback restarts the previous Python image on the same volume. That works only because the schema stays at alembic `0028` across the switch, and because Dart writes bytes Python reads (claim C3).
- **Cage-match by law** (CLAUDE.md) on every PR that touches auth, moderation, account deletion, the wire, or state lifecycle. That covers most of steps 5 to 8. The WebAuthn package is a new trust root, so it gets its own strike before anything uses it.

## 1. The problem

The gateway is 19,466 lines of Python. The reason it is Python measures at 397 lines (the bus client), and those 397 lines are firewalled. Nick ruled the port twice on 2026-10-25. Heat measured four things that shape it:

- **There is no cheap way to run Python and Dart side by side.** Account deletion is one transaction across 16 tables. Socket connections, echo suppression, the rate limiter and the topology queue live in one process's memory, and a worker lock forbids a second process. Only read-only routes and the push worker can be split off without first building #46 and #42 (RESEARCH §A2).
- **The test suite is not an oracle as it stands.** No test runs the real server, and none opens a WebSocket. About 23% could drive a binary over the wire given a seeded DB and stub servers. The rest are about 13k lines of in-process service tests (§A6).
- **Porting the gateway does not take Python off the box.** The registrar, the ChatServer, seed-building and boot migrations all run Python from the same image. Python is about 143 of its 177.5 MB (§A5).
- **Production and CI speak different chat protocols.** Prod's fork advertises `chat_server:0` with JSON payloads; CI runs geekscape main's `chat_server:1` with S-expressions. The local docker rig runs prod's exact pins (§A1, verified by hand).

## 2. The shape

### 2.1 The seam is a moment, not a route

Dart never runs as a second live writer. It is built and proven entirely on the rig, against recordings of what the Python gateway does. Then, per island, there is **one boot** at which Python stops and Dart takes `.aiko-worker.lock`. Everything that lives in process memory (the Hub, echo suppression, the topology queue, the rate limiter, the provider-token caches) moves at once, so none of it needs a cross-process version. **#46 and #42 stop being prerequisites of the port.** They remain good ideas on their own.

### 2.2 The oracle: episodes and an effect ledger, recorded from outside

An **episode** is a short scripted user journey driven over the real wire against a real binary. Examples:
- register a passkey; join a channel; send a message and see its fan-out;
- a bot posts over the bus; a moderator bans the sender; the account is deleted.

The **effect ledger** is everything the binary did to the outside world during the episode:
- each HTTP response and WS frame it sent;
- the database diff, as rows added, changed and removed, canonicalised;
- every call that reached a stub (APNs, FCM, JWKS, OAuth providers, LiveKit);
- every bus publication it made.

Recordings are made on the rig and in the harness, **from synthetic users only. No tape is ever made in production** (SPARK §fusion: the no-broker-door, no-who-called-whom and account-deletion rulings).

**Normalisation** is part of the oracle, not an afterthought:
- generated identifiers (ULIDs, challenges, tokens) are bound consistently across one performance, so "the id minted in step 2 is the id deleted in step 7" is checked, not hidden;
- timestamps come from a test clock;
- fan-out order is compared as a set where the protocol does not promise an order.

**The baseline:** Python replayed against Python must produce identical ledgers before any Dart exists (claim C1).

### 2.3 Inside Dart: parse, then a sealed Decision, then apply

The Dart gateway is built in two halves with a typed seam between them:
- **parsers** turn bytes into a `Decision`: HTTP bodies, WS frames, bus frames (`:0` JSON and `:1` S-expr), WebAuthn CBOR, provider ID tokens;
- the **applier** turns a `Decision` into effects: rows, fan-out, push, bus publishes.

`Decision` is a sealed class, a closed set we construct. This is design 15's law applied on purpose: types protect what we construct, so the applier is where the compiler pays, and it is where the island's measured defects have lived (choice versus default, source selection, all or none).

Each half gets its own kind of proof:
- **The applier** is proven by the episode ledgers, kind by kind.
- **The parsers** additionally get *differential agreement* wherever a Python reference function exists, the way the codec was fuzzed against `parser.py`:
  - bus payload decode (`payload.py`) and topology parse (`topology.py`) against Python on a fuzz corpus;
  - WebAuthn verification against the W3C L3 §16 vectors, go-webauthn's spec vectors, py_webauthn's fixtures, and our own software authenticator (`tests/test_passkey_endpoints.py:57-135`) driving both verifiers.

### 2.4 The bus: target the pin production runs, speak both dialects

**Decision: the port targets the February pin, and the bump (task #22) is NOT done first.** Bumping first couples two migrations: the bump also forces aiko_chat to `:1`, which is a bus wire change. Instead:
- The Dart gateway is a bus **observer, not an Actor**. It runs as a service-id-0 `BusProcess` that registers nothing. That removes the latent "any bus client can make the gateway post as anyone" surface (RESEARCH §A1 Q1b).
- It discovers the ChatServer by `name=chat_server` with **two explicit filters, `:0` and `:1`**, not `*`, so a future `:2` fails loudly instead of matching blind.
- It decodes both payload tiers.
- `send_message` is one `bus.send('$path/in', ...)`, which has never been exercised live from Dart. So the round trip gets its own acceptance assertion.
- New code on top of `aiko_services_dart` is roughly 150 to 200 lines.
- **Prerequisite:** CI gains a `bus-e2e` job pinned to the Dockerfile's ARGs (task #23). Today CI proves a protocol prod does not speak. The master job stays as a canary.

### 2.5 The database: drift opens the alembic DB; alembic stays the authority until after the switch

- Dart opens the live file through drift's `setup` hook:
  - assert `alembic_version == '0028'` (or the current head);
  - stamp `PRAGMA user_version` to match;
  - set `busy_timeout=5000`;
  - keep the rollback journal (WAL stays off for the restore drill).
- Tables are **hand-faithful**: `customConstraint` text copied from alembic, a `CustomSqlType('DATETIME')`, and an `IslandDateTime` that reads and **writes** `'YYYY-MM-DD HH:MM:SS.ffffff'`. Drift's default `…T…Z` would mis-sort against existing rows and could break a rollback to Python.
- **No schema freeze during the port.** Every new alembic migration is mirrored into the drift definitions in the same PR, and CI runs `validateDatabaseSchema(validateDropped: true)` against a seed built by the real alembic. A mismatch turns CI red, so the two definitions cannot silently diverge.
- **After the switch on both islands**, the drift ADR (task #16) supersedes ISL-0001: freeze `0028+` as drift's baseline, generate the snapshot, and use `make-migrations` from there. Until then, ISL-0001 stands unchanged, and so does ISL-0002 (FK off, application cascades ported as code).
- **The seed and the migrations still need Python after the switch (Fold F1/F2).**
  - **The seed** (`/app/seed/aiko.db`) is built at image build time by `build_seed.py` plus alembic. The Dart image builds it in a **Python build stage** of a multi-stage Dockerfile and copies only the file. Python is present at build, absent at runtime. It moves to Dart in the same PR as the authority change.
  - **Boot migrations** run `python -m aiko_gateway.migrate` today, and the Dart runtime has no Python. So **between an island's switch and the drift ADR taking authority, that island's schema is FROZEN**: no release in that window may carry an alembic migration. The window is kept short by doing the authority change immediately after melb. CI enforces it: a release whose image is Dart and whose alembic head differs from the frozen head fails.

### 2.6 What does not change

- **The `/v1/*` wire.** The episodes check it byte-for-byte, so the app tab has nothing to build.
- **Message bodies** stay byte-opaque, including the coming K-epoch-in-signature (app tab, 2026-10-25).
- **The deploy model:** pinned images, pulled, attested (ISL-0003).
- **What the island owns.** Upstream's planned "message persistence and replay" (`e_00_TransitionPlan.md:210`) is unbuilt. Gateway history is coupled to auth, moderation and retraction, so the island keeps owning it. If upstream replay arrives, it is one more parser into `MessageAccepted`.

### 2.7 Images

- **Two images at the switch, not before.**
  - `aiko-chat-island` becomes the Dart gateway on distroless/cc, estimated 25 to 35 MB compressed.
  - A new `aiko-bus` image carries the registrar and ChatServer at the pins. Its contents already exist: today's image minus the gateway layer.
- Both are attested on the index digest.
- Until the switch, prod keeps today's single image untouched.
- **Two pins** (`ISLAND_IMAGE`, `BUS_IMAGE`) means `image.env`, `update.sh`, the drift preflight and standup all learn a second pin. That is an open variable (§7), not a detail.

### 2.8 The WebAuthn package

- Pure Dart:
  - `pointycastle` for ES256 (strict DER→(r,s), range checks) and RS256 (minimum 2048-bit modulus);
  - `cryptography` for Ed25519;
  - `crypto` for SHA-256;
  - a narrow hand-written CBOR/COSE/authData parser: definite-length only, depth and size limits, and it returns the consumed length so the original COSE bytes are what gets stored.
- It checks more than py_webauthn 3.0.0 does:
  - reject `crossOrigin: true` and any `topOrigin`;
  - credential ID ≤ 1023 bytes;
  - one root error type that maps to a 4xx;
  - `android:apk-key-hash:` origins **derived** from the configured cert fingerprints, not a second hand-kept list.
- Formats: `none` plus `packed` self-attestation at first. Others are decided after the prod `aaguid` census (§7).
- Developed in this repo under `packages/` and **not published** until Nick confirms the name and owner.

## 3. Build order (each step useful on its own)

0. **Separable fixes, now, in Python.** Not absorbed into the port.
   - (a) The pinned `bus-e2e` job (#23).
   - (b) WebAuthn finish handlers map every py_webauthn error to 4xx (today a missing `rawId` returns 500).
   - (c) **The D1 bug, MEASURED BROKEN on the rig at prod's pins (2026-10-25).** A channel added at runtime never reaches a `channel_list` consumer. At `3e4e822`, `chat.py:263-266` aliases `share["channel_list"]` to the `channels` Category's `entries` dict. HyperSpace adds call the **Category's** `ec_producer.update` (`category.py:117-121` at a66), so the ChatServer's producer never runs `_update_consumers`. Wire evidence: the Category consumer received `(update entries.d1probe ((* d1probe * * * ()) None None))`; the ChatServer consumer received nothing. Lease renewal (300s) does not resync; only a new lease does (rediscovery or restart). Removals are missed the same way.
     - **Inferred for prod:** bus-originated messages (bots, REPL, robots) on runtime-created channels are not relayed until a resync. A deleted channel stays subscribed and keeps its row. App-sent messages are unaffected, because the gateway persists them before `bus.send`.
     - **Fix in Python first:** consume the `channels` Category's `entries` share, which does send incrementals. Then the Dart observer mirrors the FIXED behaviour. Porting first would have the episode oracle faithfully encode the bug, which is exactly C6's hazard, now measured.
     - Evidence: `evidence/d1/` (sniff of `aiko/#`, both consumers' caches, the probe script; local rig only, synthetic channel `d1probe`).
   - (d) The `aaguid` census on both islands: a read-only aggregate query, with Nick's go.
   - (e) **Pyright in CI over `src/`**, strict on `domain/` first, so the 9 `assert_never` calls become real checks (R9).
1. **Wire harness, proven against the Python binary.** Start the real uvicorn app on the rig with:
   - a seeded alembic DB and env settings;
   - recording stubs for APNs, FCM, JWKS, OAuth and LiveKit;
   - a real WS client;
   - the bus at prod pins.

   *Alone, this gives the island its first tests of the real lifespan and a real WebSocket*, and converts about 23% of the suite.
2. **Episodes and the effect-ledger recorder, with normalisation.** Gate: Python against Python is identical (C1). *Alone, it is a regression net for Python.*
3. **Dart skeleton plus drift adoption.** Hand-faithful definitions for all 22 tables, validated in CI against the alembic seed (C4). *Alone, it proves the drift ADR is feasible before anyone writes it.*
4. **Dart bus observer on the rig.** Discovery (both dialects), subscribe, `channel_list`, and a live `send_message` round trip. Ledger-equal on the bus episodes. *Alone, it is the first real exercise of `aiko_services_dart` against a ChatServer.*
5. **The applier, kind by kind:** messages, channels and memberships, the Hub and echo, then push (APNs/FCM, HTTP/2), moderation, account deletion (the 16-table cascade), reactions, DMs, calls/LiveKit. Episodes go green kind by kind.
6. **The parsers:** REST routes, WS frames, then auth. Auth uses the WebAuthn package (separately cage-matched first), social ID tokens and the OAuth broker.
7. **Coverage closure.** All episodes green, and the converted 23% wire tests green against Dart. The service-level behaviour that the 13k lines of Python tests pin is re-expressed as episodes or Dart unit tests over the applier, with a written list of what was not carried and why.
8. **Images and deploy.** Split the images and add the second pin to `image.env`, `update.sh` and the preflights. Rehearse the switch and the rollback on the rig. Then **syd**: one boot, then watch.
9. **melb**, then the drift ADR takes schema authority, and alembic and the Python gateway retire.

## 4. Tradeoffs taken

- **One switch per island instead of a gradual strangler.** All the risk concentrates in two boots. This is accepted because every step before them is proven on a rig that runs prod's exact pins, and because rollback is a pin change on an unchanged schema.
- **No production traffic in the oracle.** We lose "the last week of prod" as a specimen corpus (Noether's best property) in exchange for honouring three privacy rulings. The rig at prod pins plus scripted episodes must stand in (C6).
- **Two pins after the switch.** More deploy surface, in exchange for the size win and an honest split between our cadence and Andy's.
- **Python keeps running the bus roles.** Moving the registrar and ChatServer to Dart (gate A, or Maxwell's spark) is out of scope.

## 5. Claims to falsify

- **C1. Determinism.** Python replayed against Python gives identical normalised ledgers. If identifier binding or fan-out ordering cannot be made stable, the oracle is noise.
- **C2. Coverage.** Episodes, the converted 23% and re-expressed service tests are enough to trust a switch. The 13k lines of service tests are where this could quietly fail. The "not carried" list in step 7 is how it is checked.
- **C3. Rollback is real, in both directions.**
  - **Rows:** every row Dart writes is readable and correctly ordered by the Python gateway (dates, booleans, JSON), and Dart reads every row Python wrote, including py_webauthn's re-encoded COSE `public_key`.
  - **Sessions (Fold F3):** an access or refresh token issued by either side verifies on the other: HS256 with the shared `JWT_SECRET`, the `type` and `gen` claims, refresh-token storage. If not, every user is signed out at the switch and again at any rollback. That is the same outage twice, and it does not look like one in the logs.
- **C4. drift can model the schema faithfully.** So far only `users` has been verified. The `channels` multi-column `substr` CHECKs and the 7 rebuilt tables are unknown.
- **C5. The switch is a single boot.** No state outside SQLite and memory (sidecar files, `-journal` ownership, `.aiko-worker.lock`, the container UID) blocks Dart from taking over the volume.
- **C6. The rig is an adequate bus oracle.** Prod's pins on the rig produce the frames prod produces. D1 and the repr-shaped incremental updates are exactly where this could be false.
- **C7. The port is not a big bang wearing steps.** Steps 0 to 4 each deliver value even if the port stops there. If they don't, this is a months-long bet with no prod value until step 8.
- **C8. No schema freeze is needed WHILE BUILDING.** Mirroring each alembic migration into drift with a CI check is cheap enough that island development continues during steps 0 to 7. (After an island switches there IS a freeze, until the authority change: §2.5.)
- **C9. The moving target is affordable (Fold F4).** The Python gateway keeps shipping during the port (#21's wire change, v0.20+). Every new behaviour lands in Python with a new episode, which turns the Dart gap into a red episode instead of a forgotten difference. That is still double implementation for the port's duration. If the port runs long, the cost compounds, and the honest response is a feature freeze or R5, not pretending.

## 6. Rejected alternatives

- **R1. Strangler by route behind Caddy.** Only read-only GETs move for free, and they are the least valuable part. Any writing route first needs #46, #42 and lifting the worker lock.
- **R2. Push worker first, out of process.** Feasible, because the reaper's DELETE is race-safe. But it needs a new trigger channel that dies at the switch: machinery built to be thrown away.
- **R3. A Python decision log (Tesla's tape).** Removed in the second cast (§9).
- **R4. Bump the bus first (#22).** It couples two migrations. Speaking both dialects decouples them.
- **R5. Port only the decisions; keep the HTTP shell in Python** (CRUCIBLE's falsifier fallback). Two runtimes forever. Kept as the fallback if C2 fails.
- **R6. The island becomes the ChatServer** (Maxwell's spark). A post-switch option. It needs ADR-0007 (Andy's) and an EC producer that does not exist (#4584).
- **R7. Adopt `passkeys_server`.** It checks almost nothing (RESEARCH §A4).
- **R8. Record production traffic.** Excluded by three rulings.
- **R9. Don't port; type-check the Python (Fold F5).** The case's sharpest piece of evidence, `apns.py`'s *"`assert_never` buys editor-time errors and NOTHING IN CI"*, is literally fixed by running pyright or mypy in CI. **Measured 2026-10-25: no type checker or linter runs anywhere** (`pyproject.toml` and all workflows), and there are 9 `assert_never` calls that nothing checks. That dissolves the *types-in-CI* half of the motivation at roughly a day's cost. It does NOT dissolve:
  - one language from handset to box;
  - a compile-checked schema authority;
  - our own auditable WebAuthn verifier;
  - Nick's ruling.

  So it is rejected as a *replacement*, and **adopted as step 0(e)**: it pays for itself across the months Python keeps running, port or no port.

## 7. Open variables (not rounded to "ready")

1. **Andy** on ADR-0007 and the `aiko_services_dart` licence. Unasked. It gates step 8, not steps 1 to 7. Asking him is Nick's call (outward-facing). Fallback: vendor the roughly 1.5k lines used under the island's AGPL, which is the same question in another form.
2. **The WebAuthn package's name and owner**, before any `dart pub publish`.
3. **The two-pin deploy model:** how `image.env`, `update.sh`, the compose-drift preflight, standup and the compat guard handle `BUS_IMAGE`.
4. ~~D1~~ **Settled: broken** (step 0c). Still open: whether the fix (consume the Category share) is ours alone or also an upstream report to Andy (the ChatServer's alias is the root).
5. **The `aaguid` census:** which attestation formats prod passkeys present.
6. **drift fixes:** whether 2.34 carries #3694, #3794 and #3830, or a bump is needed.
7. **Cross-arch AOT with native assets** (`sqlite3` build hooks) from one runner, or native per-arch builds.
8. **Container UID and file ownership** on `aiko_data` across the switch.
9. **Timing.** #386 has no milestone and Nick has set none. This design does not assert one.
10. **The cost of carrying the service tests (C2, C9).** About 13k lines of in-process Python tests have to become episodes or Dart unit tests. Nobody has estimated this, and it is probably the bulk of the port. Step 7's "not carried" list is the instrument; a size estimate before step 5 is owed.
11. **The post-switch freeze window** (§2.5): how long it lasts in practice, and whether #21 or any other schema-touching work lands inside it.

## 8. Fold notes

*Movement 5, the author's own adversarial pass; folded in place above, logged here.*

- **F1. n=0 after the switch: migrations.** The Dart runtime cannot run alembic, so there is a post-switch schema freeze. Folded into §2.5 and C8.
- **F2. First arrival: the seed.** `build_seed.py` is Python. Solved with a multi-stage build. Folded into §2.5.
- **F3. Rollback of sessions, not just rows.** Folded into C3.
- **F4. Concurrent evolution.** The Python gateway keeps changing during the port. Folded as C9 and open variable 11.
- **F5. The simplest rival.** A type checker in CI dissolves part of the motivation. Measured, then folded as R9 and step 0(e). It does not re-grade the ore (Nick's ruling stands), but Temper must see it.
- **F6. Bus messages in flight at the switch.** A message published on the bus while neither gateway is subscribed is lost. Not new: any `update.sh` restart does the same today. Noted, not mitigated.
- **F7. D1 would have been encoded as truth.** An oracle recorded from the rig faithfully reproduces the rig's bugs. D1 proved it (step 0c). The general rule, folded into step 2: **an episode that records behaviour we believe is wrong is marked as a known bug, not a specification**, and Dart is not required to reproduce it.

## 9. What the second cast changed

The first cast put a `decision_log` in Python, emitted inside each writing transaction, and Dart's first job was to fold it (Tesla's spark).

The second cast removed it. Emitting constructed decisions from Python means touching every write path in 19k lines, roughly 40 write routes plus WS plus bus ingest, to produce a tape that is thrown away at the switch. And the effect ledger recorded from outside already sees every write.

What survives of Tesla's spark is the part that pays: **the sealed `Decision` lives inside Dart**, as the seam between parsers and applier. The fold-the-tape oracle becomes the episode ledger. The cost is weaker localisation: a mismatch says *which episode*, not *which decision*. That is accepted, and Dart's own parse/apply split recovers most of it.
