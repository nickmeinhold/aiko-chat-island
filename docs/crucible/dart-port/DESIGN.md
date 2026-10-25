# Design: port the island gateway to Dart, prove it with conformance tests, and switch once

*Movement 4 (Cast), round 2. 2026-10-25. Bundle: `CRUCIBLE.md` (the case), `RULINGS.md` (the decisions this rests on), `RESEARCH.md` (Heat), `SPARK.md` (the sparks and their fusion), `TEMPER.md` (round 1: RECAST 4/4, 15 deduped flaws). Every one of those flaws is folded below; §10 maps each to where it landed. §9 says what each cast changed.*

## 0. Blast radius and consent, first

- **Who this reaches.** Every user of both islands, at one moment per island: the switch.
  - **Riskiest: sign-in.** A Dart verifier that rejects a passkey Python accepted locks a person out; one that accepts an invalid assertion lets someone in as someone else. Hence §2.8 *parity first*: no policy change rides inside the port.
  - **Second: stored bytes.** Dart writes into the file Python built, and rollback needs Python to read what Dart wrote (§2.5, C3).
- **Owners.**
  - Nick decides gate G (§3) and each switch.
  - Andy is asked at step 0, not step 8, about ADR-0007 and the `aiko_services_dart` licence (outward-facing, so it is Nick's hands).
  - The app tab is told before the switch that `/openapi.json` becomes a maintained file (§2.6).
- **Throttle.**
  - Nothing runs in production before step 8.
  - syd (the test island) switches first, with named rollback triggers and a named maximum soak (§2.9). Then melb.
  - Python rollback is supported until the drift authority change, and **explicitly not after it** (§2.9).
- **Cage-match by law** on every PR touching auth, moderation, account deletion, the wire or state lifecycle, which covers most of steps 5 to 8. The WebAuthn package gets its own strike before anything uses it.

## 1. The problem

The gateway is 19,466 lines of Python. The reason it is Python measures at 397 lines (the bus client). Nick ruled the port twice on 2026-10-25. Heat measured four things that shape it:

- **No cheap coexistence.** Account deletion is one transaction across 16 tables. Sockets, echo suppression, the rate limiter and the topology queue live in one process's memory, and a worker lock forbids a second process. Only read-only routes and the push worker could split off without first building #46 and #42 (RESEARCH §A2).
- **The suite is not an oracle as it stands.** No test runs the real server or opens a WebSocket. About 23% could drive a binary given a seeded DB and stubs; about 13k lines are in-process service tests (§A6).
- **Python stays on the box anyway.** The registrar, ChatServer, seed-building and boot migrations all use Python (§A5).
- **Prod and CI speak different chat protocols:** `chat_server:0` with JSON against `chat_server:1` with S-expressions. The local rig runs prod's exact pins (§A1, verified by hand).

## 2. The shape

### 2.1 One writer, one switch

Dart never runs as a second live writer. It is built and proven on the rig, then, per island, takes over at a single, rehearsed switch. Everything held in process memory moves at once, so **#46 and #42 are not prerequisites of the port** (they remain good ideas on their own).

**The switch protocol** (fold 5):
1. Stop admission at Caddy (503 with `Retry-After` on `/v1/*` and the WS upgrade).
2. Let Python drain: in-flight requests finish, the topology queue empties, the push queue empties, all bounded by a timeout.
3. Stop Python with SIGTERM. SQLite is crash-safe, so a cut mid-transaction rolls back whole.
4. Back up the volume (the existing `update.sh` backup).
5. Start Dart. It takes `.aiko-worker.lock`, validates the schema (§2.5), and only then reports ready.
6. Re-admit traffic.

A bus message published during steps 2 to 5 is lost, exactly as during any `update.sh` restart today (F6). **The MQTT session policy is stated, not inherited:** clean session, random client id, QoS 0 subscriptions. That is what paho's defaults give at the prod pin (`mqtt.py:112`, `:230`, `:235` at `a66424d`), so the broker never redelivers into the new process. The Dart client must match it, because a persistent session would turn the switch's loss into duplication, and duplication is #42's problem.

### 2.2 The oracle: conformance tests, not universal replay

*Round 2 replaced "Python and Dart must produce identical normalised ledgers" (fold 2, Carnot's subtraction).*

An **episode** is a scripted user journey driven over the real wire against a real binary, with **its own expected results written down**. Python's behaviour today is the *reference* for "unchanged" properties, but it is not the definition of correct. The episodes cover:
- **Happy paths:** register, sign in, join, send and fan out, DM, react, call token, push.
- **Invariants with independent expected results** (fold 2):
  - Account deletion is all-or-none: a fault injected between the cascade's statements leaves either all 16 tables untouched or all purged, checked by a snapshot taken mid-episode.
  - No forbidden effect is ever observed, even transiently: no fan-out to a banned user, no push to a deleted account, no frame to a socket after disconnect.
  - Commit happens before fan-out, which happens before push and bus publish. The ledger records **the sequence** of commits and effects, not one final diff.
- **Adversarial schedules:** ban versus send, deletion versus push, refresh-token reuse, challenge replay, topology removal mid-message, an echo arriving before the local commit.
- **Faults:** kill between commit and bus publish; restart mid-episode; stub servers that fail or time out.
- **Clocks** (fold 15): an injected clock, and episodes that cross the 300s challenge TTL, the JWT expiry and the 300s EC lease.
- **The silent-desync rule** (fold 10): an episode whose request body carries an unknown field, which must succeed.

**What is compared, and how** (folds 3 and 10). Two equalities, specified rather than improvised:
- **App-observable equality.** Status codes; the JSON fields the app reads, checked against the frozen `openapi.json` schema (§2.6); WS frame types and fields; push payload keys. **Not compared:** error prose, JSON key order, pydantic's 422 `detail` text.
- **Stored-byte equality.** The `DATETIME`, `BOOLEAN`, `JSON` and `public_key` columns are compared as **raw bytes, never parsed by the comparator**. This is checked in both directions: Dart reads a Python-written alembic seed, and Python reads a copy Dart wrote. `validateDatabaseSchema` cannot do this (it compares affinity), so it is its own test.

**A harness-only decision trace** (fold 2, Tesla). In test builds, Dart records the sequence of `Decision` variants each episode produced, so a red episode names the variant that fired. It is compiled out of production, is never a table in the island database, and no tape is ever made in production (RULINGS: no broker door, no record of who called whom, account deletion). In step 5 the harness can also **inject** decisions at that seam, so the applier goes green before its parsers exist.

**Known bugs are marked, not specified** (F7): an episode that records behaviour we believe is wrong is marked as a known bug, and Dart need not reproduce it. D1 proved the need the same day.

### 2.3 Inside Dart: parse, then authorise and apply in one transaction

- **Parsers** turn bytes into a `Decision`. The list of parsers:
  - HTTP and WS bodies;
  - bus frames (`:0` JSON, `:1` S-expression);
  - WebAuthn CBOR;
  - provider ID tokens;
  - **and `Settings`** (fold 11): the 62-field env with pydantic's coercions (whitespace, None defaults, `ge=1` bounds that fail loud).
- **A `Decision` is an authenticated command with provenance**, not a verdict (fold 8). It says *who asked for what, through which door*. It does NOT carry "is allowed".
- **The applier re-checks every piece of mutable authorisation inside the owning transaction:** membership, ban, session generation, block. That is the one-door law (CLAUDE.md), and it closes the gap where a `MessageAccepted` built before a ban commits after it. External effects (fan-out, push, bus publish) become eligible only after commit, in the order §2.2 asserts.
- `Decision` is a sealed class, a closed set we construct. That is design 15's law used on purpose: the compiler pays in the applier. **Whether the island's defects actually live there in `src/` is claim C10, to be measured, not assumed** (fold 13).
- **Differential agreement** with a Python reference function is required for every parser that has one:
  - `payload.py` and `topology.py` on a fuzz corpus;
  - Settings via an env → effective-settings vector file run by both (the `test_seed_peer_vectors.py` pattern);
  - WebAuthn per §2.8.
- **Every parser obeys the silent-desync rule:** unknown fields are ignored, never rejected.

### 2.4 The bus: target the pin production runs, speak both dialects, on the share that works

- **The port targets the February pin, and the bump (task #22) is NOT done first**, because the bump would couple two migrations (the bump forces aiko_chat `:1`).
- **The Dart gateway is a bus observer, not an Actor:** a service-id-0 `BusProcess` that registers nothing. That removes the latent "any bus client can make the gateway post as anyone" surface (RESEARCH §A1 Q1b).
- **It finds the ChatServer by `name=chat_server` with two explicit filters, `:0` and `:1`**, not `*`, so a future `:2` fails loudly. It decodes both payload tiers.
- **Topology comes from the `channels` Category's `entries` share, NOT `channel_list`** (fold 7). Step 0(c) measured that `channel_list` never sends runtime adds or removes at the pin. The Python fix lands first, and the Dart observer mirrors the *fixed* behaviour.
  - Before either is trusted, the replacement path passes add, remove, reconnect, and snapshot-versus-incremental agreement on the rig. The removal frame gets captured too, since only the add frame has been seen so far.
  - The pin's f-string incrementals on other shares are a **known February-pin defect** (marked per F7), not a third dialect to emulate.
- **`send_message` is one `bus.send('$path/in', ...)`.** It has never been exercised live from Dart, so it gets its own round-trip assertion.
- New code on top of `aiko_services_dart`: roughly 150 to 200 lines.
- **Prerequisite:** a `bus-e2e` job pinned to the Dockerfile ARGs (task #23). The master job stays as a canary.

### 2.5 The database: drift opens the alembic DB; alembic stays the authority while building

- Dart opens the live file through drift's `setup` hook, **validating before writing anything** (fold 12):
  1. assert `alembic_version` is the expected head;
  2. set `busy_timeout=5000`;
  3. run `validateDatabaseSchema(validateDropped: true)` plus the stored-byte checks (§2.2);
  4. **only then** stamp `PRAGMA user_version`.

  If any step fails, it refuses to serve, and opening the file has written nothing. The rollback journal stays (WAL stays off for the restore drill).
- **Tables are hand-faithful:** `customConstraint` text copied from alembic, a `CustomSqlType('DATETIME')`, and an `IslandDateTime` that reads and **writes** `'YYYY-MM-DD HH:MM:SS.ffffff'`.
- **What this buys, stated honestly:** a *checked migration discipline* (drift snapshots, runtime validation, CI against an alembic-built seed). It is stronger than `alembic heads` run by hand, but it is **not a compile-time proof**. CRUCIBLE's claim to the contrary is corrected in place.
- **While building, there is no schema freeze.** Each new alembic migration is mirrored into drift in the same PR, and CI validates the drift definitions against a seed built by the real alembic.
- **From the first switch to the authority change, there is a freeze** (fold 6, §2.9). No release in that window may raise the alembic head, and CI enforces it. A schema-shaped bug found on syd is a **rollback, not a migration**.
- **The seed:** the Dart image builds `/app/seed/aiko.db` in a **Python build stage** (multi-stage) until the authority change, then moves it to Dart.
- **Boot migrations** need Python today, which is why the window is frozen.
- **The authority change** (task #16, superseding ISL-0001) lands right after melb's soak. It ends Python rollback (§2.9).
- ISL-0002 is unchanged: FK off, application cascades ported as code.

### 2.6 What does not change, and the one artifact that does

- The `/v1/*` wire, as the app observes it (§2.2's first equality).
- Message bodies stay byte-opaque, including the coming K-epoch-in-signature.
- The deploy model: pinned, pulled, attested images (ISL-0003).
- History stays owned by the island. Upstream's planned "persistence and replay" (`e_00_TransitionPlan.md:210`) is unbuilt; if it arrives, it is one more parser into `MessageAccepted`.
- **`/openapi.json` stops being generated** (fold 9). FastAPI generates it today: live, 76,186 bytes, and CLAUDE.md names it as the cross-tab wire contract. `shelf` generates nothing.
  - Step 1 freezes today's file into the repo.
  - Episodes validate responses against it.
  - Dart serves it.
  - CI fails if a route exists in the router but not in the file, or the reverse.
  - The app tab is told at step 1 that from the switch it becomes a maintained file, changed only in a PR both tabs see.

### 2.7 Images and the release contract, proven early

- **Two images at the switch:**
  - `aiko-chat-island` (Dart on distroless/cc, estimated 25 to 35 MB);
  - `aiko-bus` (registrar plus ChatServer at the pins: today's image minus the gateway layer).

  Both are attested on the index digest.
- **The release contract is built at step 3, not discovered at step 8** (fold 5, Carnot):
  - `image.env` gains `BUS_IMAGE`;
  - `update.sh`, the compose-drift preflight, standup and the compat guard learn both compose shapes (one image with three commands; two pins);
  - native-asset packaging (`sqlite3` build hooks) is proven for both arches.

  Every allowed upgrade and rollback transition is rehearsed on the rig before step 5 starts.

### 2.8 WebAuthn: parity first, policy later

*Round 2 rewrote this section (fold 1, all four families).*

- **At the switch, the Dart verifier's accept-set equals py_webauthn 3.0.0's plus our handlers'.** That includes formats, algorithms, `crossOrigin` handling (parsed and ignored), credential-ID length and origins. Every tightening of the earlier draft (reject `crossOrigin`, reject `topOrigin`, the 1023-byte ID cap, origins derived from cert fingerprints) is a **separate, later PR**, shipped to whichever verifier is live at the time. Never inside the switch.
- **Storage stays py_webauthn-compatible until Python rollback is retired.** `public_key` is written in py_webauthn's re-encoded COSE form, not the authenticator's original bytes, and both directions are tested (C3).
- **Evidence for what prod actually sees** (fold 1):
  - a census of `aaguid` **and of COSE `alg`**, parsed from stored `public_key` on both islands (read-only aggregates, Nick's go);
  - a **real-device matrix on a staging RP** before step 8: iOS Safari, iOS app, Android app (with `android:apk-key-hash:` origins), macOS Safari and Chrome. Each registers and signs in against both verifiers, and their `clientDataJSON` shapes are captured as test vectors.
- **Vectors:**
  - W3C L3 §16, go-webauthn's spec vectors, py_webauthn's fixtures;
  - our software authenticator, extended past P-256 `none`;
  - the device captures above;
  - existing `public_key` rows round-tripped.
- **The package:** pure Dart (`pointycastle` ES256/RS256 with strict DER and range checks; `cryptography` Ed25519; a narrow hand-written CBOR parser). Developed under `packages/`, **not published** until Nick confirms the name and owner.
- **Separable, now:** step 0(b) fixes the Python 500 on malformed finish bodies.

### 2.9 Switch, soak, rollback and the end of rollback

- **syd first.** Rollback triggers, any one of which rolls back:
  - sign-in failures above baseline;
  - any 5xx rate above baseline;
  - push accepted/failed ratio below baseline;
  - bus relay silent for longer than a channel's normal quiet period;
  - any stored-byte check failing on a nightly copy.

  Nick is the watcher, with the existing health endpoints plus one added for the triggers. **The maximum soak is named at gate G** (fold 6).
- **Within that soak:** no alembic head bump, schema work queues, and a schema bug means a rollback.
- **At the end of the soak, one of two things happens:**
  - melb switches on the same build, soaks, and then the authority change lands; or
  - syd rolls back.

  The window may not simply stay open.
- **Rollback** is a rehearsed compose transition back to the one-image shape on the same volume, schema unchanged at the frozen head. Consumed passkey challenges are harmless (300s TTL). Refresh tokens issued by either side verify on the other (C3).
- **Python rollback ends at the authority change, by design.** From then on, recovery is a forward fix or a volume restore.

## 3. Build order (each step useful on its own; one gate)

0. **Separable work, now, not absorbed into the port:**
   - (a) pinned `bus-e2e` (#23);
   - (b) the WebAuthn finish handlers map every py_webauthn error to 4xx (#17 adjacent);
   - (c) **D1: switch the Python gateway to the `channels` Category `entries` share (#24)**, proven on add, remove, reconnect and snapshot/incremental agreement;
   - (d) `aaguid` + `alg` census (Nick's go);
   - (e) pyright in CI, strict on `domain/`;
   - (f) **ask Andy** about ADR-0007 and the licence (Nick's hands);
   - (g) **measure C10**: classify the gateway's own fix history (`git log` over `src/`) into decision defects versus parse defects.
1. **Wire harness, proven against the Python binary.** Real uvicorn on the rig: a seeded alembic DB, stubs, a real WS client, the bus at prod pins, an injected clock. **Freeze `/openapi.json`** and tell the app tab. *Alone, it is the island's first real-lifespan and real-WebSocket tests.*
2. **Conformance episodes** (§2.2), including the invariant, adversarial, fault and clock classes, run against Python. **Output: the coverage map**, which links every in-memory machine and every cascade branch (echo, rate limit, topology queue, provider-token caches, the 16-table purge) to the episodes that hit it, plus **a size estimate for steps 5 to 7.** *Alone, it is a regression net for Python.*
3. **Dart skeleton, drift adoption and the release contract.** Hand-faithful definitions for all 22 tables, validated against the alembic seed; stored-byte checks both ways; both image shapes, `BUS_IMAGE`, `update.sh` and the preflights; native-asset packaging; every upgrade and rollback transition rehearsed. *Alone, it proves the drift ADR and the deploy shape are feasible.*
4. **Dart bus observer on the rig:** both dialects, Category share topology, subscribe, and a live `send_message` round trip, with the bus episodes green. *Alone, it is the first real exercise of `aiko_services_dart` against a ChatServer.*

   **GATE G (Nick's go).** Inputs: the coverage map, the size estimate, C10's measurement, Andy's answer (or its absence), and the step 3 and 4 results. It also names the syd soak maximum. **Outcomes:**
   - **go**, which starts steps 5 to 9;
   - **R5**, which ports the decisions only and keeps the HTTP shell in Python;
   - **stop.** Each of steps 0 to 4 has already paid for itself.

5. **The applier, kind by kind:** messages, channels and memberships, Hub and echo, push, moderation, account deletion, reactions, DMs, calls. Decisions are injected at the seam, and episodes go green.
6. **The parsers:** Settings, REST, WS, then auth (the WebAuthn package, struck first), social ID tokens, OAuth.
7. **Coverage closure.** Every episode green against Dart, the converted wire tests green, and a written list of what the Python service tests pinned that was not carried, and why.
8. **Staging real-device matrix, then syd:** the switch protocol (§2.1), the soak (§2.9).
9. **melb, then the authority change.** Alembic and the Python gateway retire; Python rollback ends.

## 4. Tradeoffs taken

- **Risk concentrates in two switches** instead of a gradual strangler. Accepted because every step before is proven on a rig at prod's pins, rollback is rehearsed, and the triggers are named.
- **No production traffic in the oracle**, to honour three rulings. The rig at prod pins, the staging device matrix and the census stand in (C6).
- **Two pins after the switch:** more deploy surface, in exchange for the size win and an honest split between our cadence and Andy's.
- **`/openapi.json` becomes hand-maintained.** Some drift risk, bounded by the route-parity CI check.
- **A schema freeze on each island from its switch to the authority change**, bounded by the named maximum soak.
- **Python keeps running the bus roles.** Moving them is out of scope (R6).

## 5. Claims to falsify

- **C1. Determinism where it is required.** Episodes with an injected clock and bound identifiers are repeatable against Python, including the TTL- and lease-crossing ones.
- **C2. Coverage.** The coverage map (step 2) shows every in-memory machine and cascade branch hit by at least one episode. If it cannot be written, gate G takes R5.
- **C3. Rollback is real, both ways.** Stored bytes (dates, booleans, JSON, `public_key`) and sessions (access and refresh tokens, `gen`) written by either side are read correctly by the other.
- **C4. drift models the schema faithfully:** all 22 tables, including `channels`' multi-column CHECKs and the 7 rebuilt tables.
- **C5. The switch protocol drains cleanly** within its timeout, and nothing outside SQLite and memory (sidecar files, `-journal` ownership, the lock, the container UID) blocks Dart.
- **C6. The rig plus staging is an adequate oracle.** D1 showed the rig reproduces prod's bus bugs; the device matrix covers what the rig cannot (real authenticators).
- **C7. Steps 0 to 4 pay for themselves** even if gate G says stop.
- **C8. The build needs no schema freeze; only the switch window does.**
- **C9. The moving target is affordable.** A Python change that alters observable behaviour fails CI until its episode exists. The double-implementation window is bounded by gate G's estimate.
- **C10. Defects live in decisions in `src/` too.** This is measured at step 0(g), and it orders steps 5 and 6. If parse defects dominate, the parsers go first.

## 6. Rejected alternatives

- **R1. Strangler by route behind Caddy.** Only read-only GETs move free. Writing routes need #46, #42 and lifting the worker lock.
- **R2. Push worker first, out of process.** Needs a trigger channel that dies at the switch.
- **R3. A Python decision log (Tesla's tape).** Removed in the second cast. What survives is the harness-only decision trace (§2.2).
- **R4. Bump the bus first.** It couples two migrations.
- **R5. Port only the decisions; keep the HTTP shell in Python.** Kept, and now a **named gate-G outcome**.
- **R6. The island becomes the ChatServer** (Maxwell's spark). Post-switch, needs ADR-0007 and an EC producer.
- **R7. Adopt `passkeys_server`.** It checks almost nothing.
- **R8. Record production traffic.** Excluded by three rulings.
- **R9. Don't port; type-check the Python.** It dissolves the `assert_never` motivation (no type checker runs anywhere today; 9 `assert_never` calls are unchecked). It does not dissolve the rest, or Nick's ruling. Adopted as step 0(e).
- **R10. Universal deterministic replay, with Python as the oracle for everything** (round 1's §2.2). Replaced by conformance tests with independent expected results. Python agreement cannot prove atomicity, absence of forbidden effects, or race safety (Carnot, Noether).

## 7. Open variables (not rounded to "ready")

1. **Andy's answer** on ADR-0007 and the licence. Asked at step 0(f); it feeds gate G. Fallback: vendor the roughly 1.5k lines used under the island's AGPL.
2. **The WebAuthn package's name and owner**, before `dart pub publish`.
3. **The syd soak maximum**, named at gate G.
4. **The `aaguid` and `alg` census results**, and what the staging device matrix captures.
5. **drift fixes** (#3694, #3794, #3830) in 2.34, or a bump.
6. **Native-asset cross-compilation,** or per-arch native builds (resolved at step 3).
7. **Container UID and file ownership** across the switch (resolved at step 3's rehearsal).
8. **Timing.** #386 has no milestone; this design asserts none.
9. **The size of steps 5 to 7.** Unknown until step 2; it is gate G's main input.
10. **Whether the D1 fix is ours alone,** or also an upstream report to Andy (the ChatServer's alias is the root).

## 8. Fold notes (movement 5, round 1; still true)

F1 the post-switch migration freeze · F2 the seed via a Python build stage · F3 session rollback · F4 the moving target · F5 R9 · F6 the in-flight bus loss, now stated policy (§2.1) · F7 known-bug episodes.

## 9. What each cast changed

- **Cast 1 → cast 2:** removed the Python `decision_log`. The sealed `Decision` lives only inside Dart.
- **Cast 2 → round 2 (after temper r1):**
  - the oracle went from *identical normalised ledgers* to *conformance tests with independent expected results plus two specified equalities* (a subtraction: most normalisation machinery is gone);
  - WebAuthn went from *stricter* to *parity first*;
  - the switch became a protocol with triggers;
  - the freeze window was bounded;
  - the bus moved to the Category share;
  - `Decision` became a command whose authorisation is rechecked in-transaction;
  - `/openapi.json` and `Settings` were priced;
  - gate G was added, with R5 as a named outcome.

## 10. Temper r1 → where each flaw landed

| # | flaw | landed in |
|---|---|---|
| 1 | WebAuthn policy inside a port | §2.8, §0, step 8 matrix |
| 2 | oracle blind to atomicity, ordering, races | §2.2 invariants, schedules, faults, trace |
| 3 | normaliser erases rollback bytes | §2.2 stored-byte equality, C3 |
| 4 | no size estimate or stop | step 2 map + estimate, gate G, C2, C9 |
| 5 | no release-state model | §2.1 protocol, §2.7, §2.9, step 3 |
| 6 | incoherent freeze window | §2.5, §2.9 |
| 7 | step 4 on the broken share | §2.4, step 0(c) |
| 8 | stale pre-authorised Decision | §2.3 |
| 9 | `/openapi.json` disappears | §2.6, step 1 |
| 10 | byte-for-byte; silent-desync | §2.2 equalities, §2.3 |
| 11 | Settings unlisted | §2.3 |
| 12 | drift overclaim; stamp-before-validate | §2.5, CRUCIBLE corrected |
| 13 | unmeasured "defects in decisions" | C10, step 0(g) |
| 14 | Andy asked last | step 0(f), gate G |
| 15 | clocks | §2.2, C1 |
