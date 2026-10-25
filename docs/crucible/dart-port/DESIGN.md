# Design: replace the gateway's command with a Dart one, in the image that already exists

*Movement 4 (Cast), round 3, **plus post-round-3 folds marked [r3-fold, UNSTRUCK]**: temper r3 was RECAST 4/4 with the round budget spent, so those folds have not faced a strike. 2026-10-25. Bundle: `CRUCIBLE.md`, `RULINGS.md`, `RESEARCH.md`, `SPARK.md`, `TEMPER.md` (r1: RECAST 4/4, 15 flaws; r2: RECAST 4/4, subtractive). §9 says what each round changed; §10 maps r2's flaws.*

**What round 3 is.** Round 2's panel told the design to subtract, and it does. The port no longer changes the image layout, the deploy contract or the schema authority. **Dart replaces one process (the gateway) inside today's image.** Everything else on the island stays as it is: alembic, the registrar, the ChatServer, `update.sh`, one pin. The design is smaller than round 2's by intent; where it is not, §9 says why.

## 0. Blast radius and consent, first

- **Who it reaches.** Every user of an island, at that island's switch.
  - Riskiest is sign-in (§2.8: parity, never policy).
  - Then stored bytes Python must still read on rollback (§2.5).
- **What is NOT behind the switch (fold r2-12).** Steps 0(b) and 0(c) change PRODUCTION behaviour now, in Python, each under its own review:
  - 0(c) is a **state-lifecycle change with a hard-delete path** (§2.4): cage-match by law.
  - Nothing *of the port* runs in production before step 8.
- **Owners.**
  - Nick: gate G, each switch, the `aiko_services_dart` licence (§7.1), and any narrowing of the WebAuthn registration policy (§2.8).
  - Andy is asked at step 0 as Nick's chosen courtesy.
  - The app tab is told at step 1 that `/openapi.json` becomes a maintained file.
- **Throttle.**
  - syd first, then melb, each switchable back independently at any time (§2.9), because nothing about the schema or the image changes.
- **Cage-match by law** on auth, moderation, account deletion, wire and state-lifecycle PRs. The WebAuthn package gets its own strike first.

## 1. The problem (unchanged facts)

- **No cheap coexistence.** One 16-table deletion transaction; in-memory sockets, echo, rate limiter and topology queue; a worker lock (RESEARCH §A2).
- **The suite is not an oracle as it stands** (§A6).
- **Python stays on the box** for the registrar, ChatServer, seed and migrations (§A5).
- **Prod speaks `chat_server:0`** with JSON; CI speaks `:1` with S-expressions. The rig runs prod's pins (§A1).

## 2. The shape

### 2.1 One image, one more executable, one switch

- **Today's image gains the Dart gateway executable**, AOT-compiled with its native SQLite asset, beside the Python one.
- The compose `gateway` service keeps its image. Its **entrypoint runs alembic exactly as today** (`python -m aiko_gateway.migrate`) and then `exec`s **either** the Python gateway **or** the Dart one, chosen by one setting in the operator's `.env` (`GATEWAY_IMPL=python|dart`, default `python`).
- Rollback is the same setting flipped back on the same volume.
- **There is no `BUS_IMAGE`, no second compose shape, no change to `update.sh`, the preflights or standup** (fold r2-2).
- The size win from a Dart-only image is **deferred**, as a separate later decision that needs the Python gateway to be gone first.

**The switch is a restart, the same as every release today** (fold r2-5):
1. **[r3-fold, UNSTRUCK] The failure and rollback contract** (temper r3 #6):
   - the entrypoint **never falls back**: a Dart crash is a crash, as today;
   - configure **commits, then restarts, then verifies**, and a failed verification does not revert, so rollback stays reachable while the gateway is down;
   - **the switch is proved by the new process's identity** (the implementation it reports), not by a serve-check that the old process could pass;
   - the one-image-many-roles audit (HEALTHCHECK, entrypoint, env defaults) holds for both implementations.

   The mechanism: change the setting with a **new `island configure --gateway-impl` option**. Today configure edits only named keys (`--name`, `--seed-peers`, the passkey switch, `--domain`; `configure.dart:103-116`), so adding the option is a small step 3 PR. `GATEWAY_IMPL` also has to be forwarded by compose, under invariant 7b. Configure already rewrites only that line, proves it with `docker compose config`, and checks the island serves afterwards.
2. Restart the gateway service.
3. Python receives SIGTERM, and uvicorn closes connections and drains in-flight requests within its graceful timeout.

Two losses are **accepted exactly as on every restart today**:
- in-process push retries are dropped;
- bus messages published while no gateway is subscribed are lost.

Neither is migrated, retried or duplicated.

**[r3-fold, UNSTRUCK] HTTP never waits on the bus** (temper r3 #1, all four families). Dart binds its port once it holds `.aiko-worker.lock` and has opened the DB (§2.5), exactly as Python does. The bus is not a precondition for sign-in, history or DMs. At the prod pin, a stale retained `(primary found …)` can stall discovery forever (RESEARCH §A1 Q1c), and Caddy does no health checking (checked), so a bus-gated listener would be a permanent island-wide 502.

What a partial view could harm is made impossible at its producer instead:
- **topology reconcile is disarmed** until one full snapshot from the single configured source has been applied;
- **bus-dependent actions** (publish, relay subscription) are refused until then.

`/health` may report the bus; the listen socket never waits on it.

**MQTT policy, stated:** clean session, random client id, QoS 0. This matches paho's defaults at the pin (`mqtt.py:112,230,235` at `a66424d`), so no session survives the restart and nothing is redelivered.

### 2.2 The oracle: a fixed set of journeys, each with its own obligations

*Fold r2-9: no injectable Decision seam, no decision trace, no every-branch coverage map.*

**[r3-fold, UNSTRUCK] Fifteen journeys** (temper r3 #3: the first ten did not cover what steps 5 and 6 build). **Any family without a journey stays out of the Dart binary syd runs.** The clock is injected through a test-only control compiled out of production builds (temper r3 #10). Driven over the real wire against a real binary (uvicorn or Dart) on the rig at prod's pins, with an injected clock. Each has written obligations: a **success** case, a **rejected** case, and where relevant a **race** or **fault** case, each with its own expected result. Python's behaviour is the reference only for "unchanged".

| # | journey | obligations beyond success |
|---|---|---|
| 1 | register a passkey | malformed body → 4xx; challenge replay → rejected; expired challenge (TTL crossed) → rejected |
| 2 | sign in | wrong origin → rejected; sign-count regression → rejected; refresh reuse → rejected; JWT expiry crossed |
| 3 | join a channel | non-member reads → rejected; ban, then join → rejected |
| 4 | send and fan out | ban vs send race (linearisation rule below); unknown request field → accepted (silent-desync) |
| 5 | echo | a bus echo of our own send is not stored twice; an echo arriving before the local commit → **exactly one stored row** *(r3-fold: expected world named)* |
| 6 | DM and reactions | non-participant → rejected |
| 7 | delete an account | **all-or-none**: kill the process during the cascade → all 16 tables untouched or all purged; push to the deleted account → none |
| 8 | push | device registered → wake planned; recipient budget exceeded → throttled |
| 9 | topology | Category add → subscribed + row; Category remove → per §2.4's rule; reconnect snapshot that omits a channel → no hard-delete |
| 10 | kill after commit | commit, then kill before bus publish → **row kept, publish dropped, no retry, no duplicate** (fold r2-6) |
| 11 | moderation *(r3-fold)* | ban → the target's sockets disconnect and no later frame reaches them; a report → visible to moderators only; a takedown fans out a retraction |
| 12 | calls *(r3-fold)* | call token for a member → issued; for a non-member → rejected; occupancy read; ring wake planned within the recipient budget |
| 13 | social sign-in *(r3-fold)* | valid Apple/Google ID token (stub JWKS) → session; wrong audience, expired or replayed nonce → rejected; JWKS rotation mid-episode |
| 14 | OAuth broker *(r3-fold)* | the code flow completes; state replay → rejected; handoff after expiry → rejected |
| 15 | recovery re-key *(r3-fold)* | re-key → every existing socket disconnected and old refresh tokens rejected; the new passkey signs in |

**The linearisation rule** (fold r2-6):
- A message's effects (fan-out, push, publish) are computed from the transaction that committed it and dispatched after that commit.
- A revocation (ban, delete, session bump) that commits **later** does not recall effects already computed. It does disconnect the revoked sockets, so **no frame computed after the revocation's commit reaches them.**
- Journeys 3, 4 and 7 test that exact interleaving with barriers.

**Two equalities, specified:**
- **App-observable:** status codes; the JSON fields the app reads, validated against the frozen `openapi.json` request and response schemas (§2.6); WS frame types; push payload keys. Not error prose, not key order.
- **Stored bytes:** `DATETIME`, `BOOLEAN`, `JSON`, `public_key`, compared raw, never parsed by the comparator. Checked **in CI**, in both directions: Dart reads an alembic-built seed, and Python reads a copy Dart wrote. **Not on the boot path**, where Python is not present to read.

**Known bugs** (fold r2-7): an exception list is frozen before gate G. Each entry has its own expected behaviour and an approval. **No entry may cover stored bytes, the WebAuthn accept-set, or anything the app reads.** New entries go through the same review.

### 2.3 Inside Dart: parsers write nothing; one transaction does everything stateful

- **Parsers** turn bytes into typed commands, and **write nothing**: HTTP and WS bodies, bus frames (`:0`, `:1`), WebAuthn CBOR, ID tokens, and `Settings`. `Settings` is checked by an env → effective-settings vector file run by both implementations.
- **Each command is handled by one service function that owns one transaction** (fold r2-1, Tesla). Inside it:
  - re-check every piece of mutable authorisation (membership, ban, block, session generation);
  - **consume the WebAuthn challenge**;
  - insert the credential or update the sign count;
  - bump the session generation;
  - write the rows.

  Effects are dispatched only after commit (§2.2). This is the one-door law, and the list of stateful steps is this short on purpose.
- **Commands are ordinary sealed values**, not a framework. The compiler checks exhaustive handling, which is design 15's law where it pays. Whether defects actually live there in `src/` is **C10, measured at step 0(g)**.
- **Silent-desync:** unknown fields in HTTP and WS JSON are ignored. *Narrowed* (Carnot): this does not extend to cryptographic structures, where WebAuthn parsing follows §2.8.

### 2.4 The bus

- **The Feb pin is the target; no bump first.**
- **Observer, not Actor:** service id 0, registers nothing.
- **[r3-fold, UNSTRUCK] Discovery matches ONE protocol** (temper r3 #5). The production build derives it from the pinned aiko_chat (`:0` at `3e4e822`, read from the Dockerfile ARG at build time). That is the Dart equivalent of Python importing `get_server_service_filter()` from the co-installed package. `:1` exists only behind a canary build flag for the master `bus-e2e` job.
  - The two-dialect producer of a double match is gone from production.
  - On zero or two matches, **the bus capability is never granted:** no publish, no subscribe. This shows in the logs §2.9's triggers read. It is a refusal, not a health flag nothing reads.
- **[r3-fold, UNSTRUCK] A bus `remove` can no longer destroy history** (temper r3 #2, producer first). Today `remove` → `hard_delete_channel` + messages + memberships (`client.py:169-172` → `main.py:136`), which has been dormant only because runtime removes never arrive (D1). **Recommendation to Nick:** a bus `remove` UNSUBSCRIBES and marks the channel `removed_upstream`, and never deletes. Deliberate deletion goes through the owning service, attended. The trigger is external; the destruction was ours, and this makes it impossible.
- **One topology source at a time.** The port uses whichever source production runs when step 4 starts.
  - The Category `entries` switch (#24) is a **separate, separately reviewed Python change** that carries its own green cases and lands only with the no-delete rule above. It is off the port's critical path.
  - A periodic `channel_list` re-lease (a full snapshot each lease, measured in D1) is a *replacement* source, **never a second live subscription**.
- **`send_message`** is one `bus.send('$path/in', …)`, with a live round-trip assertion.
- **New code** on `aiko_services_dart`: roughly 150 to 200 lines. CI gets a `bus-e2e` job pinned to the Dockerfile ARGs (#23).

### 2.5 The database: Dart is a client of the alembic file, never its owner

*Fold r2-3: no adoption, no stamp, no drift migrations during the port.*

- **Alembic stays the sole schema authority, ISL-0001 unchanged.** The entrypoint runs it at every boot, as today, before either gateway starts. So there is **no freeze window and no rollback cliff**: a new migration ships in a release exactly as now, and both gateways must read it.
- **Dart opens the file as a client:**
  - `busy_timeout=5000`, rollback journal;
  - no `PRAGMA user_version` write, no `onCreate`, no migrations;
  - before serving, it asserts `alembic_version` equals the head it was built for, and refuses otherwise.

  **[r3-fold] The query layer is `package:sqlite3` directly** unless drift shows a demonstrated query benefit (Carnot r3: migration machinery with no migrations is just another refusal path). Step 3's probe asks only "does opening ever write?". **The expected head is read from the image's own alembic at build time**, never a hand constant (temper r3 #8).
- **Schema oracle, in CI:** a checked-in `sqlite_master` dump from the alembic-built seed. Every alembic migration PR updates the dump, and Dart's queries are tested against that seed. That is all. No hand-faithful 22-table mirror is needed while alembic owns the schema.
- **`IslandDateTime`** reads and writes `'YYYY-MM-DD HH:MM:SS.ffffff'`.
- **ISL-0002 is unchanged:** FK off, application cascades as code.
- **Moving schema authority to drift (#16) is a SEPARATE decision**, with its own readiness gate (a rehearsed first drift-owned migration, seed creation and recovery). It is not part of this port. **This narrows Nick's 09:51 "let's do the ADR"**: it is surfaced for him to confirm, not silently overridden (§7.2).

### 2.6 `/openapi.json`

- FastAPI generates it today: live, 76,186 bytes, and it is the cross-tab wire contract.
- Step 1 freezes it into the repo. The journeys validate **request and response schemas** against it (Carnot: route-name parity alone is not schema parity). The Dart gateway serves the file.
- CI fails on any route present in one but not the other.
- The app tab is told at step 1.

### 2.7 (removed)

Two images, `BUS_IMAGE` and the two-pin release contract are gone (fold r2-2). See §2.1.

### 2.8 WebAuthn: split authentication from registration; parity in both, policy never

*Fold r2-1, all four families.*

**Authentication** (existing passkeys signing in). **[r3-fold]** It always implements the full ES256/RS256/EdDSA ceiling; the `alg` census orders the work and never shrinks the sign-in accept-set, because a key registered after the count would be locked out (temper r3 #7). It needs only:
- signature verification for the algorithms **actually stored**: census `alg` from `public_key` on both islands (ES256, RS256 and EdDSA are the ceiling);
- the same `clientDataJSON`, `rpIdHash`, flag and sign-count checks py_webauthn 3.0.0 makes.

Parity is defined by py_webauthn's **own fixtures plus ours**, including:
- non-canonical DER signatures, which py_webauthn accepts, so Dart accepts them too;
- **its EdDSA header patch**;
- its COSE re-encoding, read back.

"Strict DER", reject `crossOrigin`, reject `topOrigin` and the 1023-byte cap all move to the later-policy list.

**Registration** (new passkeys) is where the accept-set is big: py_webauthn verifies whatever `fmt` arrives (none, packed, tpm, apple, android-key, android-safetynet, fido-u2f), with no trust roots except Apple's. Two honest options, **Nick's call** (§7.3):
- **(a)** **[r3-fold] Reproduce py_webauthn's ACTUAL accept-set, including its non-checks.** With no trust roots configured, it parses each format and chains certificates only for Apple. That is far smaller than "implement every format" (temper r3 #7, Noether), and it is the honest size for gate G's estimate.
- **(b)** *Before* the port, in a separate reviewed Python PR, narrow registration to what the islands actually receive. Then the Dart parity target is the narrowed set.

  `fmt` is not stored today. With `attestation` unset (so "none" is requested), platform passkeys from Apple and Google send `fmt: none`. So (b) likely means `none` plus `packed`, and the evidence is a short period of logging the received `fmt` and `aaguid` as aggregate counts, with Nick's go.

The rest:
- **Storage stays py_webauthn-compatible** (re-encoded COSE) for as long as `GATEWAY_IMPL=python` is a possible rollback, which in this design is indefinitely.
- **Real-device vectors** (fold r2, Maxwell): syd's own RP (it is the test island), a test account, and Nick's own iOS, Android and macOS devices. Assertions are captured against Python and verified offline in Dart. These are tester bytes, not user traffic. No staging RP and no new app build.
- **The package:** pure Dart (`pointycastle`, `cryptography`, a narrow CBOR parser) under `packages/`. **Not published** until Nick confirms the name and owner.
- **Step 0(b)** fixes the Python 500 on malformed finish bodies, now.

### 2.9 Switch, soak, rollback

- **syd first.** Baselines are measured on Python on syd during steps 1 to 3, read from **existing logs only** (no new endpoint; the island declines to know).
- **Rollback triggers**, read from the same logs, each with a window and a count floor so one event on a small island is not an infinite rate:
  - sign-in failures over N in M minutes above baseline;
  - 5xx over N in M minutes above baseline;
  - **positive bus heartbeat lost:** the observer's subscription is up and a sync has been applied within the lease period. This replaces "relay silent", which cannot tell a quiet channel from a dead relay.

  N and M are set at gate G.
- **Rollback** is `GATEWAY_IMPL=python` plus a restart, rehearsed on the rig: the same image, the same schema, at any time, on either island independently. **There is no point at which rollback ends** while alembic is the authority, so melb can switch or switch back without touching syd. That dissolves r2's melb-rollback gap (Noether).
- **The soak** has a maximum, named at gate G. At its end, either melb switches or syd rolls back.

## 3. Build order

0. **Now, separable, each under its own review:**
   - (a) pinned `bus-e2e` (#23);
   - (b) WebAuthn finish → 4xx;
   - (c) **Category share (#24), cage-match by law, with Nick's hard-delete ruling**;
   - (d) the `alg` census, plus, if option (b) is chosen, aggregate `fmt`/`aaguid` counting;
   - (e) pyright in CI;
   - (f) Andy asked (Nick);
   - (g) measure C10;
   - (h) **#21 is unaffected**: there is no freeze, so it ships whenever the app tab is ready.
1. **Wire harness against Python** (real uvicorn, real WS, stubs, injected clock, the bus at pins). **Freeze `/openapi.json`**; tell the app tab.
2. **The ten journeys against Python**, with their obligations. **Output: an estimate for steps 5 to 7**, with each journey's Dart size counted from the Python services it touches.
3. **Dart skeleton:** opens the alembic seed as a client (the write probe from §2.5), the `sqlite_master` CI oracle, and stored-byte CI both ways. **[r3-fold, UNSTRUCK] CI** proves the executable builds into the image for both arches (native-asset packaging) and is selectable with `GATEWAY_IMPL`, but **published release images exclude the Dart gateway until Nick's licence act** (temper r3 #4: presence in an attested AGPL image is itself the selection §7.1 forbids, and `GATEWAY_IMPL=python` is not the act).
4. **Dart bus observer on the rig:** both dialects, exactly-one assertion, Category topology, send round trip.

   **GATE G (Nick).** Inputs: the estimate, C10, Andy's answer or its absence, the steps 3 and 4 results, the WebAuthn registration choice (§2.8). Outcomes:
   - **go**;
   - **stop** (fold r2-11). Python stays, keeping the harness, the ten journeys, pyright and the D1 fix, all of which have already paid for themselves.

   Gate G also fixes the soak maximum, N and M, and the known-bug list.
5. **Services, journey by journey:** messages and channels, echo, push, moderation, deletion, reactions, DMs, calls.
6. **Parsers:** Settings, REST, WS, then auth (the package, struck first), ID tokens, OAuth.
7. **All ten journeys green against Dart**, plus the converted wire tests, plus a written list of what the Python service tests pinned and how each item is carried or why it is not.
8. **Device vectors on syd's RP, then syd:** `GATEWAY_IMPL=dart`, soak.
9. **melb.** The Python gateway's code is deleted only by a later, separate decision; until then, rollback is one setting.

## 4. Tradeoffs taken

- **The image stays Python-sized** (about 177 MB) during and after the port, because the bus needs it anyway. The size win waits for a separate decision.
- **Both gateways stay in the image** until a later decision removes Python's. That is a larger attack surface for that period, in exchange for rollback that never ends.
- **Alembic stays the schema authority.** Drift's checked migrations wait for #16.
- **No production traffic in the oracle.** Tester bytes on syd's RP stand in for real devices.
- **`/openapi.json` becomes hand-maintained**, bounded by the CI route and schema check.

## 5. Claims to falsify

- **C1.** The ten journeys are repeatable against Python with an injected clock.
- **C2.** The ten journeys' obligations cover what an island user can observe at a switch. The step 7 list says what they do not.
- **C3.** Stored bytes and sessions written by either gateway are read correctly by the other (CI, both ways).
- **C4.** Dart can open the alembic file without ever writing on open (step 3 probe).
- **C5.** **[r3-fold]** The switch is an ordinary restart. Dart binds after the lock and the DB open; topology reconcile and bus actions wait for the first snapshot, and HTTP never does. Falsified if any route needs bus state to answer correctly before the snapshot lands.
- **C6.** The rig plus syd's-RP device vectors is an adequate oracle.
- **C7.** Steps 0 to 4 pay for themselves if gate G says stop.
- **C8.** Alembic at every boot, with two gateways reading one schema, adds no freeze and no cliff.
- **C9.** **[r3-fold, corrected: the round-3 sentence was false]** Both gateways must meet every journey **until a later, separate decision deletes the Python gateway**. That is not bounded by gate G. CI fails unless both binaries pass. Gate G bounds the *build*, not this agreement.
- **C10.** Defects live in decisions in `src/` (measured at 0g; it orders steps 5 and 6).

## 6. Rejected alternatives

- **R1.** Strangler by route.
- **R2.** Push worker first.
- **R3.** A Python decision log.
- **R4.** Bump the bus first.
- **R5.** A Python HTTP shell over Dart decisions. It needs an FFI/IPC boundary and a second transaction owner (Carnot r2), so it is no longer an outcome; gate G's fallback is "stop".
- **R6.** The island as the ChatServer.
- **R7.** `passkeys_server`.
- **R8.** Production tapes.
- **R9.** Type-check instead of porting. Adopted as 0(e), not as a replacement.
- **R10.** Universal replay.
- **R11 (new).** Two images and two pins (round 2's §2.7). Too much deploy surface for a win that needs the Python gateway gone first.
- **R12 (new).** Drift adoption and authority as part of the port (round 2's §2.5). It created the freeze window, the rollback cliff and an adoption path that did not run as written. It is decoupled into #16.

## 7. Open variables

1. **The `aiko_services_dart` licence.**
   - **Checked:** all 52 commits are Nick's, and upstream `aiko_services` is Apache-2.0, which permits derivatives with attribution and can be included in an AGPL-3.0 work.
   - So this is **Nick's decision**, with Andy asked first as his chosen courtesy (2026-09-20).
   - **Silence selects nothing:** the dependency enters a release only after Nick licenses it.
2. **#16 narrowed.** Nick ruled "let's do the ADR" (09:51). This design moves the drift authority change OUT of the port. It is still to be done, as its own decision with its own readiness gate. **To confirm with Nick.**
3. **WebAuthn registration: implement all formats, or narrow Python first** (§2.8). Nick's call, at gate G at the latest.
4. **Nick's hard-delete ruling for #24** (§2.4). **[r3-fold] Recommended answer:** a bus `remove` unsubscribes and marks `removed_upstream`, never deletes; deletion is attended, through the owning service.
5. **The package's name and owner**, before publishing.
6. **The soak maximum, N and M, and the known-bug list.** Set at gate G.
7. **The query layer:** drift with migrations off, or `sqlite3` (step 3 probe).
8. **Native-asset packaging** for both arches (step 3).
9. **Timing.** No milestone is asserted.

## 8. Fold notes

F1 to F7 from round 1 still hold where they still apply:
- F1 and F6 are dissolved: alembic still runs at boot, and restart losses are the same as today's.
- F2 is dissolved: the seed is unchanged.
- F3 holds (C3).
- F4 holds (C9).
- F5 holds (R9).
- F7 is bounded (§2.2).

## 9. What each round changed

- **Cast 1 → 2:** the Python decision log was removed.
- **Round 2 (after temper r1):** conformance tests replaced replay; WebAuthn parity-first; the switch protocol; the freeze bounded; the Category share; in-transaction re-checks; `/openapi.json` and Settings priced; gate G.
- **Round 3 (after temper r2), subtractive:**
  - one image and `GATEWAY_IMPL` replace two images and two pins;
  - alembic stays the authority and drift becomes a client, which deletes the freeze, the rollback cliff and the 22-table mirror;
  - ten journeys with obligations replace the coverage map, the decision trace and the injection seam;
  - the switch is an ordinary restart with ready-after-snapshot;
  - WebAuthn is split into authentication (small) and registration (Nick's call);
  - R5 is replaced by "stop".
- **Post-round-3 folds [UNSTRUCK]:**
  - HTTP never waits on the bus (the round-3 ready gate was a total-outage path, found by all four);
  - a bus `remove` cannot hard-delete (recommended to Nick);
  - one topology source at a time, with Category off the critical path;
  - one discovery protocol per build;
  - the failure and rollback contract;
  - fifteen journeys;
  - published images exclude Dart until the licence;
  - the sign-in ceiling and the reframed option (a);
  - C9 corrected;
  - `package:sqlite3`.
- **Added in round 3:** the linearisation rule, the exactly-one discovery assertion, the Category fallback, bounded known bugs, count-floored triggers and a positive heartbeat. Each answers a named r2 flaw.

## 10. Temper r2 → where each flaw landed

| # | flaw | landed |
|---|---|---|
| 1 | WebAuthn scope bomb; transactional home | §2.8 auth/reg split + Nick's call; §2.3 one transaction |
| 2 | deploy over-complication | §2.1 one image + `GATEWAY_IMPL`; R11 |
| 3 | authority change not needed; adoption path broken | §2.5 client-only; R12; §7.2 |
| 4 | licensing fallback | §7.1, checked against fact |
| 5 | drain cannot converge; premature ready | §2.1 restart + ready-after-snapshot |
| 6 | linearisation; kill-after-commit | §2.2 rule + journey 10 |
| 7 | unbounded known-bug mark | §2.2 frozen list, forbidden on bytes, accept-set, app-read |
| 8 | triggers undefined | §2.9 windows, floors, heartbeat, logs |
| 9 | Decision seam, trace, coverage map | §2.2 ten journeys; §2.3 plain sealed values |
| 10 | D1 hard-delete; Category unmeasured; exactly-one | §2.4, journey 9, fallback, assertion |
| 11 | R5 not cheap | R5 replaced by "stop" |
| 12 | #21 freeze; §0 throttle | no freeze (0h); §0 rewritten |
