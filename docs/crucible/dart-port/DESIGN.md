# Design: replace the gateway's command with a Dart one, in the image that already exists

*Movement 4 (Cast), round 3, plus post-round-3 folds `[r3-fold]` (struck in temper round 4) and post-round-4 folds `[r4-fold]`. 2026-10-25. Temper: r1 RECAST 4/4 (15 flaws), r2 RECAST 4/4 (subtractive), r3 RECAST 4/4 (10, budget spent), r4 RECAST 4/4 on the r3 folds (12, none a total outage). **Status: PROVISIONAL.** Nick chose to apply the r4 folds without a fifth strike and go to Blade; gate G is the next real check. The r4 folds are mostly deletions (the `removed_upstream` mark, the snapshot refusal and disarm, the SHA→protocol map, the "stays out" rule). Bundle: `CRUCIBLE.md`, `RULINGS.md`, `RESEARCH.md`, `SPARK.md`, `TEMPER.md`. §9 says what each round changed; §10 maps r2's flaws.*

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

- **The image gains the Dart gateway executable**, AOT-compiled with its native SQLite asset, beside the Python one. **[r4-fold]** Until Nick's licence act (§7.1) that Dart-inclusive image is a CI artifact only; **published images carry Python alone**. Every image declares the gateways it carries in an OCI label written at build (`cc.imagineering.island.gateways=python` or `python,dart`).
- The compose `gateway` service keeps its image. Its **entrypoint runs alembic exactly as today** (`python -m aiko_gateway.migrate`) and then `exec`s **either** the Python gateway **or** the Dart one, chosen by one setting in the operator's `.env` (`GATEWAY_IMPL=python|dart`, default `python`).
- Rollback is the same setting flipped back on the same volume.
- **There is no `BUS_IMAGE`, no second compose shape, no change to `update.sh`, the preflights or standup** (fold r2-2).
- The size win from a Dart-only image is **deferred**, as a separate later decision that needs the Python gateway to be gone first.

**The switch is a restart, the same as every release today** (fold r2-5):
1. **[r3-fold; r4-fold] The failure and rollback contract** (temper r3 #6, r4 #2):
   - **configure cannot select a gateway the image lacks:** `--gateway-impl` refuses any value missing from the gateways label of the image pinned in `image.env`. The same check runs when configure is re-run after a runbook image rollback. So the crash-loop cell (a Dart setting on a Dart-less image, under no-fallback and no-revert) cannot be produced. If the entrypoint is somehow asked to exec an absent gateway anyway, it exits naming the cause (`GATEWAY_IMPL=dart but this image carries: python`);
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

**[r3-fold; r4-fold] HTTP never waits on the bus, and neither does any command** (temper r3 #1, all four families; r4 #4). Dart binds its port once it holds `.aiko-worker.lock` and has opened the DB (§2.5), exactly as Python does. The bus is not a precondition for sign-in, history, DMs or sending. At the prod pin, a stale retained `(primary found …)` can stall discovery forever, for Python and Dart alike (RESEARCH §A1, line 48), and Caddy does no health checking (checked). So a bus-gated listener would be a permanent island-wide 502.

**Nothing is refused for want of a snapshot.** What a partial view could harm is made impossible at its producer: bus topology can change **subscriptions only, never rows or visibility** (§2.4), so a partial or stale view can at worst leave a subscription missing until the next snapshot. A send commits, fans out locally, and publishes **best-effort**, exactly as Python does today: in a stall the publish is dropped, with journey 10's semantics (no retry, no duplicate).
- *Rejected in r4:* refusing bus-effect commands before commit until a snapshot lands (Noether and Tesla r4). In a stall that can last forever, that makes **sends fail island-wide**: round 3's total-outage path, moved onto the most-used route.
- *Named tradeoff (Nick):* cross-client relay is silently lost during a bus stall, as today. It is detected by §2.9's positive-heartbeat trigger.

`/health` may report the bus; the listen socket never waits on it.

**MQTT policy, stated:** clean session, random client id, QoS 0. This matches paho's defaults at the pin (`mqtt.py:112,230,235` at `a66424d`), so no session survives the restart and nothing is redelivered.

### 2.2 The oracle: a fixed set of journeys, each with its own obligations

*Fold r2-9: no injectable Decision seam, no decision trace, no every-branch coverage map.*

**[r3-fold; r4-fold] The journey set covers every route family** (temper r3 #3; r4 #3, #10). **Every route family in the frozen `/openapi.json` (§2.6) has a journey before gate G's estimate.** A CI check maps each openapi path prefix to a journey id and fails on any orphan, so the set's size is derived, not asserted. *(The r3 rule "a family without a journey stays out of the Dart binary" is deleted: one process serves the island, so a missing family would 404 on syd, which is R1 entering through a side door, and §2.6's route parity already forbids it.)* The clock is injected through a test-only control compiled out of production builds (temper r3 #10). Driven over the real wire against a real binary (uvicorn or Dart) on the rig at prod's pins, with an injected clock. Each has written obligations: a **success** case, a **rejected** case, and where relevant a **race** or **fault** case, each with its own expected result. Python's behaviour is the reference only for "unchanged".

| # | journey | obligations beyond success |
|---|---|---|
| 1 | register a passkey | malformed body → 4xx; challenge replay → rejected; expired challenge (TTL crossed) → rejected |
| 2 | sign in | wrong origin → rejected; sign-count regression → rejected; refresh reuse → rejected; JWT expiry crossed |
| 3 | join a channel | non-member reads → rejected; ban, then join → rejected |
| 4 | send and fan out | ban vs send race (linearisation rule below); unknown request field → accepted (silent-desync) |
| 5 | echo | a bus echo of our own send is not stored twice; an echo arriving before the local commit → **exactly one stored row** *(r3-fold: expected world named; r4: the serializer is the single DB isolate (§2.5) plus a `UNIQUE(channel_id, client_msg_id)` key (#42), not the in-memory content set)* |
| 6 | DM and reactions | non-participant → rejected |
| 7 | delete an account | **all-or-none**: kill the process during the cascade → all 16 tables untouched or all purged; push to the deleted account → none |
| 8 | push | device registered → wake planned; recipient budget exceeded → throttled |
| 9 | topology | add → subscribed (+ row if new; idempotent on an existing row); remove → unsubscribed, row, history and visibility unchanged; a snapshot that omits a channel → the same as remove; a re-add → re-subscribed, no duplicate row *(r4-fold)* |
| 10 | kill after commit | commit, then kill before bus publish → **row kept, publish dropped, no retry, no duplicate** (fold r2-6) |
| 11 | moderation *(r3-fold)* | ban → the target's sockets disconnect, and no frame computed after the ban's commit reaches them (the linearisation boundary below; r4); a report → visible to moderators only; a takedown fans out a retraction |
| 12 | calls *(r3-fold)* | call token for a member → issued; for a non-member → rejected; occupancy read; ring wake planned within the recipient budget |
| 13 | social sign-in *(r3-fold)* | valid Apple/Google ID token (stub JWKS) → session; wrong audience, expired or replayed nonce → rejected; JWKS rotation mid-episode |
| 14 | OAuth broker *(r3-fold)* | the code flow completes; state replay → rejected; handoff after expiry → rejected |
| 15 | recovery re-key *(r3-fold)* | re-key → every existing socket disconnected and old refresh tokens rejected; the new passkey signs in |
| 16 | recovery enrol and policy *(r4-fold)* | enrol → status reflects it; policy read by the owner only; a second passkey added (`passkey/add`) signs in |
| 17 | communities *(r4-fold)* | create, discover, join; a non-member reads a private community → rejected |
| 18 | blocks *(r4-fold)* | block (symmetric, `is_blocked_between`) → each side's messages and reactions are filtered from the other's reads (`not_blocked_predicate`) and WS fan-out; a DM video token between them → rejected (`livekit.py:134`); unblock restores all of it |
| 19 | signing keys *(r4-fold)* | publish a key for one's own account → readable at `/keys/{pubkey}`; publishing for another account → rejected |
| 20 | island surface, read-only *(r4-fold)* | `/island`, `/islands`, `/gateways`, `/capabilities`, `/health`, `.well-known`, terms and privacy → bytes match the frozen schema; `/register` and `/login` per `open_registration` |

The orphan check is the authority; this table is its current reading (route decorators counted 2026-10-25).

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
- **[r3-fold; r4-fold] Discovery matches ONE protocol** (temper r3 #5; r4 #5, #6). **At process start, Dart reads the protocol version constant from the co-installed `aiko_chat` package in this image**: the same file the image's ChatServer runs, and the same source Python's `get_server_service_filter()` imports from. *(r4: the r3 wording "derived from the Dockerfile ARG" needed a SHA→protocol table, a second source that can drift from the installed package. Deleted.)* `:1` exists only behind a canary build flag, bound to the unpublished `bus-e2e` artifact so a release recipe cannot set it.
  - The two-dialect producer of a double match is gone from production.
  - **While** the match count is not exactly one, the bus capability is **withheld**: no publish, no subscribe. It is granted when the count becomes one, and re-evaluated on every registrar update. **It is never latched.** Zero matches is the ordinary state when the gateway boots before the ChatServer, and two is the ordinary transient when the ChatServer restarts inside its lease. A latch would turn compose start order into a process-lifetime bus outage. The state shows in the logs §2.9's triggers read.
- **[r3-fold; r4-fold] Bus topology has authority over SUBSCRIPTIONS only, never over channel rows** (temper r3 #2; r4 #1, all four). Today `remove` → `hard_delete_channel` + messages + memberships (`client.py:169-172` → `main.py:136`), which has been dormant only because runtime removes never arrive (D1). **Recommendation to Nick:**
  - a bus `remove`, or a snapshot that omits a channel, **unsubscribes, and nothing more**: no row change, no mark, no visibility change;
  - an `add` re-subscribes, and is idempotent on an existing row;
  - deliberate deletion goes through the owning service, attended.

  The gateway's SQLite is the sole persister (HyperSpace is a service graph, not a datastore), so upstream topology is a *relay attachment*, not channel existence. The trigger is external; the destruction was ours, and this makes it impossible.
  - *Removed in r4:* the r3 `removed_upstream` mark. It had no reader and no exit, and it left a writable channel with a dead relay.
  - *Rejected in r4:* Tesla's "omission hides the channel from every read". A ChatServer restart with a partial list (runtime channels are not in its snapshot, D1) would make live channels vanish for users: a fold adding an outage producer.
  - With rows out of the bus's reach, a partial or stale view can at worst leave a subscription missing until the next snapshot. So reconcile needs **no disarm-until-snapshot**, and that r3 mechanism is deleted.
- **One topology source at a time.** The port uses whichever source production runs when step 4 starts.
  - The Category `entries` switch (#24) is a **separate, separately reviewed Python change** that carries its own green cases and lands only with the subscriptions-only rule above. It is off the port's critical path.
  - A periodic `channel_list` re-lease (a full snapshot each lease, measured in D1) is a *replacement* source, **never a second live subscription**. **[r4-fold]** One owner holds a source's subscriptions and its queued callbacks. On replacement, the old owner is retired (unsubscribed, queue dropped) before the new owner applies its snapshot. A straggler could only add or drop a subscription, which the next snapshot heals.
- **`send_message`** is one `bus.send('$path/in', …)`, with a live round-trip assertion.
- **New code** on `aiko_services_dart`: roughly 150 to 200 lines. CI gets a `bus-e2e` job pinned to the Dockerfile ARGs (#23).

### 2.5 The database: Dart is a client of the alembic file, never its owner

*Fold r2-3: no adoption, no stamp, no drift migrations during the port.*

- **Alembic stays the sole schema authority, ISL-0001 unchanged.** The entrypoint runs it at every boot, as today, before either gateway starts. So there is **no freeze window and no rollback cliff**: a new migration ships in a release exactly as now, and both gateways must read it.
- **Dart opens the file as a client:**
  - `busy_timeout=5000`, rollback journal;
  - no `PRAGMA user_version` write, no `onCreate`, no migrations;
  - before serving, it asserts `alembic_version` equals the head it was built for, and refuses otherwise.

  **[r3-fold; r4-fold] The query layer is `package:sqlite3` directly** (Carnot r3: migration machinery with no migrations is just another refusal path). Step 3's probe asks only "does opening ever write?".
  - **The expected head is read at process start from the alembic tree the entrypoint just ran**, never a hand constant and never baked in a separate build job (temper r3 #8; r4 #7: RESEARCH §A5 builds binaries and image in different jobs). The image build fails if that tree has more than one head.
  - **One dedicated database isolate** owns the connection (r4 #8, Noether). `package:sqlite3` is synchronous FFI, so on the main isolate journey 7's 16-table cascade would freeze HTTP, WS and the bus. The isolate is also the natural home of §2.3's one-transaction rule: writers are serialised by construction.
  - **One write helper** owns the stored spellings (`IslandDateTime`, BOOLEAN, JSON). Raw `execute` is not reachable from command code (r4, Tesla). Stored-byte CI stays the witness, run on the queries steps 5 and 6 actually add.
- **Schema oracle, in CI:** a checked-in `sqlite_master` dump from the alembic-built seed. Every alembic migration PR updates the dump, and Dart's queries are tested against that seed. That is all. No hand-faithful 22-table mirror is needed while alembic owns the schema.
- **`IslandDateTime`** reads and writes `'YYYY-MM-DD HH:MM:SS.ffffff'`.
- **ISL-0002 is unchanged:** FK off, application cascades as code.
- **Moving schema authority to drift (#16) is a SEPARATE decision**, with its own readiness gate (a rehearsed first drift-owned migration, seed creation and recovery). It is not part of this port. **Nick confirmed this narrowing of his 09:51 "let's do the ADR" on 2026-10-25 ~13:45** (§7.2).

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
- **Rollback** is `GATEWAY_IMPL=python` plus a restart, rehearsed on the rig: the same image, the same schema, at any time, on either island independently. An *image* rollback (the runbook) to a tag whose label lacks `dart` requires `GATEWAY_IMPL=python` first; configure enforces it (§2.1, r4). **There is no point at which rollback ends** while alembic is the authority, so melb can switch or switch back without touching syd. That dissolves r2's melb-rollback gap (Noether).
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
2. **The full journey set against Python** (every openapi route family; orphan check green), with their obligations. **Output: an estimate for steps 5 to 7**, with each journey's Dart size counted from the Python services it touches.
3. **Dart skeleton:** opens the alembic seed as a client (the write probe from §2.5) from its DB isolate, the `sqlite_master` CI oracle, and stored-byte CI both ways. **[r3-fold; r4-fold] CI** proves the executable builds into the image for both arches (native-asset packaging) and is selectable with `GATEWAY_IMPL`, but **published release images exclude the Dart gateway until Nick's licence act** (temper r3 #4: presence in an attested AGPL image is itself the selection §7.1 forbids, and `GATEWAY_IMPL=python` is not the act). Also in step 3: the gateways label, configure's refusal against it, and **a CI boot of the published (Python-only) digest at the default**, so the digest that ships is a digest that was tested.
4. **Dart bus observer on the rig:** the one protocol read from the installed package, the never-latched exactly-one capability, subscriptions-only topology, send round trip.

   **GATE G (Nick).** Inputs: the estimate, C10, Andy's answer or its absence, the steps 3 and 4 results, the WebAuthn registration choice (§2.8), and **the licence act or a date for it** (r4: step 8 cannot run a published Dart image without it). Outcomes:
   - **go**;
   - **stop** (fold r2-11). Python stays, keeping the harness, the journey set, pyright and the D1 fix, all of which have already paid for themselves.

   Gate G also fixes the soak maximum, N and M, and the known-bug list.
5. **Services, journey by journey:** messages and channels, echo, push, moderation, deletion, reactions, DMs, calls.
6. **Parsers:** Settings, REST, WS, then auth (the package, struck first), ID tokens, OAuth.
7. **The full journey set green against Dart** (orphan check green), plus the converted wire tests, plus a written list of what the Python service tests pinned and how each item is carried or why it is not.
8. **Precondition: Nick's licence act (§7.1)**, so a published image carries Dart. **Device vectors on syd's RP, then syd:** `GATEWAY_IMPL=dart`, soak.
9. **melb.** The Python gateway's code is deleted only by a later, separate decision; until then, rollback is one setting.

## 4. Tradeoffs taken

- **The image stays Python-sized** (about 177 MB) during and after the port, because the bus needs it anyway. The size win waits for a separate decision.
- **Both gateways stay in the image** (once it is published with Dart, after the licence act) until a later decision removes Python's. That is a larger attack surface for that period, in exchange for rollback that never ends.
- **[r4-fold] The dual-write tax (Nick).** From gate G's go until Python is deleted, every feature (v0.10.0 wire work, #21's per-device field, #3096 agent identity) is written twice for CI to pass. **Forcing function:** the end of the melb soak forces a dated keep-or-delete-Python decision, so "a later, separate decision" has a date.
- **[r4-fold] Relay loss in a bus stall (Nick).** Sends commit and fan out locally, and the publish is dropped, exactly as Python does today (§2.1). Detected by the positive-heartbeat trigger.
- **Alembic stays the schema authority.** Drift's checked migrations wait for #16.
- **No production traffic in the oracle.** Tester bytes on syd's RP stand in for real devices.
- **`/openapi.json` becomes hand-maintained**, bounded by the CI route and schema check.

## 5. Claims to falsify

- **C1.** The journey set is repeatable against Python with an injected clock.
- **C2.** The journey set's obligations cover what an island user can observe at a switch: every openapi route family has one (orphan check). The step 7 list says what they do not.
- **C3.** Stored bytes and sessions written by either gateway are read correctly by the other (CI, both ways).
- **C4.** Dart can open the alembic file without ever writing on open (step 3 probe).
- **C5.** **[r3-fold; r4-fold]** The switch is an ordinary restart. Dart binds after the lock and the DB open, and nothing waits for a snapshot: topology changes only subscriptions, and publish is best-effort as in Python. Falsified if any route answers differently from Python before the first snapshot lands.
- **C6.** The rig plus syd's-RP device vectors is an adequate oracle.
- **C7.** Steps 0 to 4 pay for themselves if gate G says stop.
- **C8.** Alembic at every boot, with two gateways reading one schema, adds no freeze and no cliff.
- **C9.** **[r3-fold, corrected: the round-3 sentence was false]** Both gateways must meet every journey **until a later, separate decision deletes the Python gateway** (dated at the end of the melb soak, §4). That is not bounded by gate G. CI fails unless both binaries pass. Gate G bounds the *build*, not this agreement.
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
2. ~~#16 narrowed, to confirm~~ **DECIDED (Nick, 2026-10-25 ~13:45): the drift ADR is a SEPARATE decision**, out of the port. Alembic stays the schema authority through the port. #16 keeps its own readiness gate: a rehearsed first drift-owned migration, seed creation and recovery.
3. **WebAuthn registration: implement all formats, or narrow Python first** (§2.8). Nick's call, at gate G at the latest.
4. **Nick's hard-delete ruling for #24** (§2.4). **[r4-fold] Recommended answer:** bus topology controls subscriptions only. A `remove` or an omission unsubscribes and changes nothing else (no mark); deletion is attended, through the owning service.
5. **The package's name and owner**, before publishing.
6. **The soak maximum, N and M, and the known-bug list.** Set at gate G.
7. ~~The query layer~~ **Settled (r3/r4): `package:sqlite3` in one DB isolate** (§2.5).
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
- **Post-round-3 folds (struck in r4):**
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
- **Post-round-4 folds (Nick: apply, no fifth strike), mostly subtractive:**
  - bus topology controls subscriptions only: no `removed_upstream` mark, no disarm-until-snapshot (all four);
  - nothing refused for want of a snapshot; publish best-effort as in Python (the panel's refuse-before-commit fix was rejected as round 3's outage path on the send route);
  - the gateways image label plus configure's refusal close the crash-loop cell; the licence act is a gate G input and a step 8 precondition; CI boots the published digest;
  - discovery reads the protocol from the installed package at start, and the capability is never latched;
  - the alembic head is read at process start; one DB isolate; one write helper;
  - the journey set is derived from the openapi orphan check (journeys 16 to 20 added; the "stays out" rule deleted); residue swept;
  - the dual-write tax is named, with a dated keep-or-delete decision at the end of the melb soak.
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
