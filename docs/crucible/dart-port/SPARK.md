# Spark: the out-breath

*Movement 3. 2026-10-25. One wild spark per family, generated in parallel and blind to each other. Seed: `CRUCIBLE.md` plus `RESEARCH.md` §A, §B and §D. §C (solution space) was withheld from the three external seats. **Maxwell's spark is the author's own and is NOT blind to §C**, so weigh it accordingly. No axes were assigned. The sparks landed on four different ones (bus wire, time, decisions, bus identity), so no re-throw was needed. Sparks are reproduced verbatim.*


---

### Noether (DeepSeek)

## Spark
**One line:** Build a Dart bus-dialect compiler that taps live production broker traffic and emits the gateway as a generated, sealed client—so the port targets the observed February dialect, not a library.

**The shape:** A passive MQTT tap runs beside the Python gateway for one week. It subscribes `#`, filters the four surfaces (discovery add/remove, per-channel subscribe payloads, `channel_list` share traffic, and `send_message` calls), and stores raw frames with timestamps. From that corpus, a generator emits: (1) a sealed `BusFrame` union for every observed shape, including the a66 f-string repr, JSON chat payload, and `chat_server:0` filter; (2) golden fixtures keyed by operation; (3) a Dart client whose only dependency is an MQTT transport. The Python gateway’s 397 lines become the reference *behavior*, not the reference *code*. Swap order: Dart subscribes read-only and logs decoded parity against the Python process; then it takes over discovery and subscribe; then EC consume; then the single proxy call. The seam is the broker, which already carries both processes without a shared SQLite writer.

**Why it is alive:** The bus is a running wire protocol, and prod has been speaking the target dialect every day. The port stops being “reimplement `aiko_services_dart` and hope the pins match” and becomes “compile the traffic.” Version skew, unlicensed dependencies, and Actor liability fall outside the generated surface because none of them appear in the captured frames. The oracle is not the test suite; it is the last production week.

---

### Carnot (Codex/GPT)

## Spark
**One line:** Port the island through time: build a replayable island-in-a-bottle, then let Dart inherit the next moment.

**The shape:** Put a flight recorder around the Python gateway’s boundary. It records ordered arrivals—HTTP requests, WebSocket frames, bus deliveries, timer firings—and supplies clock values, randomness and external-service replies through recorded inputs. Each recording begins with a consistent SQLite snapshot.

Dart inhabits a second bottle: its own database, its own sockets, its own entire gateway. Feed it the same arrivals. Every outward action lands in an effect ledger: responses, disconnects, fanouts, bus publications, push requests and database changes. Nothing escapes the bottle.

The unit of migration becomes an **episode**: register a passkey, join a channel, send a message, receive its echo, ban the sender, delete the account. An episode crosses every internal boundary it needs. Its recording becomes an executable specimen that both implementations must perform. Preserve meaningful ordering and exact bytes where they carry meaning; bind generated identifiers consistently across the performance.

Build the Dart gateway episode by episode, with `shelf`, the new WebAuthn package and the database code growing inside this apparatus. Every discovered edge case becomes another specimen.

Finally, perform the handover at an episode boundary: stop admission, drain Python’s pending work, seal a snapshot, release the worker lock, and start Dart as the sole live owner. Keep the recorder running.

**Why it is alive:** The seam is **between moments**. Account deletion stays one transaction. The Hub, echo suppression and topology queue travel together. Python and Dart coexist with separate worlds and one public voice. The port’s accumulating asset is a collection of little inhabited islands—behaviour you can rewind, inspect and make the next implementation live through.

---

### Tesla (Grok)

## Spark

**One line:** Keep Python as the only parser of the messy world, and port the island as a pure fold over a sealed decision tape that Python appends inside the same transaction as its writes.

**The shape:** Add one table, `decision_log` (`seq`, `name`, `body`), inserted in the transaction that already mutates the island. Each row is an S-expression the gateway constructs: `MessageAccepted`, `EchoSuppressed`, `AccountPurged`, `ChannelReconciled`, `PushPlanned`, `CredentialStored`, `AuthenticationRejected`, `OperatorElected`. A closed set. The body is ours, so a Dart sealed class describes something we built.

The Dart binary's first command is `island fold`. It reads a snapshot copy plus the tape and rebuilds a second database from seq 0. It never opens the live file, never takes the worker lock, never speaks MQTT. The proof is row identity against Python's database after the same requests. The existing HTTP tests stay in Python and grow a postcondition: run, then fold, then diff. Kinds go green one at a time. `AccountPurged` is the long one, because it is the 16-table transaction written as a single decision.

Cutover is one boot. Python stops applying. The Dart process takes `.aiko-worker.lock` and becomes the only applier, writing SQL we constructed onto the file Alembic already built. HTTP, WebAuthn CBOR, and the bus decoder move last, as pure functions from bytes to a decision, with the software authenticator and a captured bus transcript as their inputs. `chat_server:0` JSON and `chat_server:1` S-expressions both become `MessageAccepted` before Dart ever sees them, so the fold never learns a protocol version.

**Why it is alive:** The island's recurring defects are decisions mistaken for values, and types only protect values you construct. The tape is the constructed layer, sitting exactly where the parse ends. Both falsifiers answer themselves: the suite is not the oracle, the tape is, and production never has two writers because the sequence lives in the proof, not in a second process. The protocol version you would otherwise hard-code is the one fact the fold refuses to know.


---

### Maxwell (Claude, the author)

## Spark (Maxwell / Claude, the author; NOT blind to RESEARCH §C, which the other three never saw)

**One line:** Don't port the gateway to talk to the ChatServer; port the island so that it IS the ChatServer, so the bus divergence and the 143 MB both disappear at once.

**The shape:** The research says the gateway is a strange guest on its own island. It discovers a ChatServer that runs in its own compose, subscribes to topics that ChatServer publishes, and reads a `channel_list` share that ChatServer owns. That share is in fact a mirror of the gateway's own `channels` table, and it is currently broken for runtime adds at the prod pin. Every message an app user sends goes out to the bus and comes back as an echo that content-keyed dedupe has to suppress.

So invert it. The Dart island is ONE process that registers on the bus as `chat_server` itself:
- It answers `send_message` from bots and REPLs (inbound becomes a method call, not a topic subscription).
- It publishes to `{topic}/{channel}` for every message it persists.
- It owns the `channel_list` share as an EC PRODUCER whose source of truth is the SQLite `channels` table. That share is written by the same code that writes the row, so it cannot drift.
- With `aiko_services_dart`'s registrar (gate A, 14/14 at the prod pin), the island also runs its own registrar in-process.

Python leaves the island entirely. That removes the registrar and chat containers, the editable installs and the 143 MB. Andy's Python ChatServer does not die: it becomes the ORACLE. The Dart island is differentially tested against it the way the codec was fuzzed against `parser.py`: the same bot session is driven against both, and what each publishes is diffed.

**Order:**
1. EC producer in Dart (#4584).
2. Dart island answers `chat_server:0` on the local rig, beside the Python one with the Python one stopped, and the gateway acceptance suite passes.
3. Differential harness against Andy's ChatServer.
4. Drop the containers.

**Why it is alive:** the echo-suppression hack (#42's stand-in), the D1 runtime-add bug, the `:0`/`:1` protocol split, the "Actor is an RCE surface" finding and the mixed-runtime image are all one bug: *the island's source of truth lives in one process and its bus identity lives in another.* Merge them and five open problems become zero.


---

## The fusion (bisociation)

**Decomposed to function and sockets:**

| spark | function | input socket | output socket | where the seam is |
|---|---|---|---|---|
| Noether | derive the bus client from what prod actually says | raw broker frames | sealed frame union + golden fixtures | the broker |
| Carnot | make behaviour replayable; migrate by *episode*; hand over at one moment | boundary arrivals + recorded outside replies | an effect ledger | **between moments** |
| Tesla | make decisions explicit and closed; Dart's first job is a *fold* | a decision tape written in the writing transaction | a rebuilt DB, proven by row identity | where parsing ends |
| Maxwell | merge the source of truth with the bus identity | — | — | inside one process |

**The shared anchor (three of four, reached blind).** Noether, Carnot and Tesla never saw §C's strangler-by-route menu, and none of them proposed a route split. All three chose the same skeleton: **Dart never runs as a second live writer; it lives in its own world, is fed recordings, and takes over at a single moment.** The oracle is a recording, not the test suite. That convergence is evidence against the route-split strangler, which §A2 had already shown needs #46 and #42 before any writing route can move.

**The preserved seam: two tapes, split along design 15's law.** Carnot records *inputs* (bytes that get parsed). Tesla records *decisions* (values we construct). Design 15's temper said types protect values you construct and do nothing for values you parse. These two sparks cut the island exactly along that line, so the port gets two oracles of different kinds:

- **The construct half (decision → effects).** The applier: rows written, sockets fanned out, push planned, bus published. Its oracle is *fold the decision tape and diff row identity plus the effect ledger.* This is where sealed types pay, and where the island's measured defects lived. **Port this first.**
- **The parse half (bytes → decision).** HTTP and WS bodies, bus frames (`:0` JSON and `:1` S-expr), WebAuthn CBOR, provider ID tokens. Its oracle is *differential agreement*: the same input specimen must produce the same decision in Python and Dart, the way the codec was fuzzed against `parser.py`. Types do not help here, so agreement is the proof. **Port this second.**
- **The cutover is one boot** (Carnot's "between moments", Tesla's "one boot"). Python stops, Dart takes `.aiko-worker.lock`. There is never a second live writer, so ISL-0002's application cascades, the Hub, echo suppression and the topology queue move *together*, and #46/#42 are not prerequisites of the port.

**The third object, which no spark contained:** *the Decision is the port's seam.* The Python gateway emits a closed set of constructed decisions. Dart is built in two halves against two oracles of different kinds. The switch happens once.

**Oh, of course:** *port the decisions against a tape, port the parsers against a corpus, and switch once: the oracle splits exactly where types stop helping.*

**What the fusion refuses from its own sparks (a constraint, not a critique):**
- **No tape is made in production.** Noether's week-long prod tap, and Tesla's `decision_log` as a live prod table, both collide with recorded rulings:
  - "no broker door: the island declines to know" (2026-09-05);
  - "no record of who called whom" (2026-09-01);
  - account deletion, since an `AccountPurged` row that outlives the account is a trace of the deleted user.
  
  Tapes are produced on the rig and in the wire harness, from synthetic episodes. The fusion loses Noether's best property, "the oracle is the last production week". It keeps that only as the far weaker *"the rig runs prod's pins"* (RESEARCH §A1). Temper should test whether that is enough.
- **Maxwell's spark does not fuse.** Its anchor (one process owns both truth and bus identity) is orthogonal, and it expands scope into ADR-0007, which is Andy's, and into an unbuilt EC producer (#4584). It is carried forward as a *post-cutover* option and a rejected alternative, not folded in. If it is right, it is right after the switch, not before.
