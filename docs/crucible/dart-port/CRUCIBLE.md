# Crucible: port the island gateway from Python to Dart

*Movement 1 (Ore). 2026-10-25. Target named explicitly by the invocation (#386, task #15). The consent gate was crossed by Nick's ruling at 09:51 the same day: Dart `island standup`/`configure` first, THEN a crucible on the port plan.*

## The ore, verified

- **#386** (public, OPEN, no milestone): *Migrate the island from Python to Dart*. Measured there: 19,466 lines in `src/`, 27,885 in `tests/`, 2,195 in `alembic/` (29 migrations).
- **Rulings that bind it:** `shelf`, not `dart_frog` (2026-09-26); port the island NOW (2026-10-25 00:49); write and publish our own Dart WebAuthn server package rather than adopting `passkeys_server` (09:51; confirm the name and owner before `dart pub publish`); the gateway goes to Dart while `aiko_services` and `aiko_chat` stay Python (09:54).
- **Prior verdicts that bind it by shape, not just verdict:**
  - Design 15 DISSOLVED 4/4 (`docs/design/15-TEMPER.md`): *types protect values you CONSTRUCT and do nothing for values you PARSE.*
  - The cage-match Dart rewrite RECAST 4/4 and is still unbuilt: a smaller target than this one.
  - No `docs/crucible/*/TEMPER.md` covers this candidate, so it is not a re-forge.

## What the scout found that the handoff did not carry

1. **ADR-0007, "Porting Aiko Services to other languages", is RESERVED for Andy** (`aiko_chat_app/docs/adr/index.md`, status "Andy's research in progress"). Keeping `aiko_services` in Python and talking to it from a Dart gateway is a position inside the space Andy owns. It does not block the gateway port, which is our code. But "the Dart gateway speaks the bus through `aiko_services_dart`" makes `aiko_services_dart` a production dependency before Andy has said what a non-Python aiko is.
2. **`aiko_services_dart` still has NO LICENCE** (no `LICENSE` file, checked 2026-10-25), and its last commit is `0442ebf` on 2026-09-20, five weeks idle. The Apache-2.0 ruling was gated on asking Andy first, and that question is still unasked. An AGPL island (decided 2026-07-17) that bundles an unlicensed library has a licensing hole, not just an untidy one.
3. **The gateway's whole bus surface is four operations** (`src/aiko_gateway/aiko/client.py`):
   - **discovery:** `do_discovery(ChatServer, filter)` with add/remove callbacks;
   - **subscribe:** one topic per channel, `{server_topic_path}/{channel}`;
   - **EC consume:** `channel_list` on `{server_topic_path}/control`;
   - **one proxy call:** `chat_server.send_message(username, recipients, message)`.

   The gateway is an `aiko.Actor`, but nothing found so far calls INTO it. `aiko_services_dart` has the codec, transport, EC consumer, `services_cache`, a dispatcher and a registrar. No `Actor`. Whether the port needs one is a Heat question, not an assumption.
4. **The Python suite is not a ready-made parity oracle.** 37 of 79 test files drive the gateway over HTTP/WS, but only **5** do so without also monkeypatching or opening DB sessions. 1,540 test functions. So "run the existing tests against the Dart binary" is unavailable as the suite stands.
5. **Bus versions.** Production pins `aiko_services` `a66424d` and the `aiko_chat` fork `3e4e822` (`Dockerfile:26-28`). CI `bus-e2e` runs geekscape master of both. The sibling checkout `../aiko_services` is at `03f7b1c`, but only the gateway's editable install (local pytest) uses it. **CORRECTED by Heat, then verified by hand:** the local docker rig (`aiko-registrar-1`, `aiko-chat-1`) runs `/opt/aiko_services` at `a66424d` and `/opt/aiko_chat` at `3e4e822`, which ARE production's pins. Gate A swapped that container, so its 14/14 **does** speak to the February pin. The earlier line here ("it says nothing about the Feb pin") was wrong.

   **What IS divergent, and worse than "three versions":** the fork advertises `chat_server:0` (`chat.py:79`, `_VERSION = 0`) while geekscape main advertises `chat_server:1` (`protocol.py:37`). The service filter is an exact string match. So CI `bus-e2e` exercises a protocol production does not speak. The Python gateway escapes this by calling `aiko_chat`'s own `get_server_service_filter()`, so it matches whatever is installed beside it. A Dart gateway cannot import that function, so it must hard-code the version.

## Why this one, with heat

The island is one language away from being one system. The app is Dart. `deploy/dart/` is Dart now, and v0.19.0 shipped two attested Dart binaries yesterday. The only Python left that we wrote is the gateway, and the reason it is Python measured at 397 lines out of 19,238. Every recurring defect the island has paid for lately was a *decision* mistaken for a value: choice versus default, which source wins, all or none. A sealed class makes those compile errors. `apns.py` says so in its own docstring: *"the `assert_never` calls below buy editor-time errors and NOTHING IN CI."*

**What it would change:**
- A contributor (the reason CI exists) reads one language from handset to box.
- The schema authority becomes compile-checked (`drift`) instead of `alembic heads` run by hand.
- The image drops a Python runtime plus two git-cloned editable installs.
- The passkey trust root becomes code we wrote and can read, rather than a dependency we never audited.

**The spark:** *the bus is the seam the island was already built to cut at.* The port does not have to be a rewrite. It can be a sequence of swaps, each proven against the thing it replaces.

## The falsifier (what would prove this is slag)

- **If the Dart gateway cannot reach behavioural parity without first rewriting the 27,885-line test suite in Dart**, then the port costs roughly 2.4× the code it replaces, buys no user-visible change, and arrives as a big bang. In that case the honest move is to port the *decisions* only (push planning, ring gating, operator-mode election) and leave the HTTP shell in Python.
- **Second falsifier:** the swap-by-swap story needs a seam where Python and Dart can run at the same time. The island has one SQLite file and FK-off application cascades (ISL-0002). If no seam exists that does not put two writers on one database, the "sequence of swaps" is a fiction and the port really is a big bang.

## Scores

- **aliveness 3:** Nick ruled "do it now" twice in one day, three memory files point at it, and two Dart binaries already ship.
- **impact 2:** it removes a whole class of hand-run guard (alembic heads, `assert_never` that CI never runs) and a runtime from the image. It is developer- and operator-facing, with no user-visible change. That is stated, not rounded up.

## Output location

`docs/crucible/dart-port/` on branch `docs/crucible-dart-port`.
