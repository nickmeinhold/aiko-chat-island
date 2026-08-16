# DESIGN — `livekit://` DataScheme (Cast)

Status: **RE-CAST after Temper round 1 — UN-STRUCK (needs a round-2 strike before build).**
Target repo: `aiko_services` (Andy Gelme's) via fork PR — NOT a unilateral commit.

## TEMPER ROUND 1 — verdict + re-cast (2026-08-11, PR #125)

**Unanimous REQUEST_CHANGES** from 3 cross-family adversaries (Kelvin/Gemini,
Carnot/GPT, Tesla/Grok; Wu/Kimi dark on a 402 billing error — 3 seated, gate met).
The strike CONVERGED, and the candidate's VISION survived ("LiveKit *as* aiko
transport is the right direction" — all three) but its MECHANISM was overturned.
Binding re-cast decisions folded in below (the original in-process shape is kept
further down for provenance, struck-through in intent):

- **[REFRAME — flips the default] The in-process asyncio `Room` is NOT the spine.**
  The PRIMARY falsifier FIRED: `scheme_zmq` teardown is safe only because
  `zmq.RCVTIMEO` forces the blocking recv to observe the terminate flag — asyncio +
  native WebRTC/FFI threads give no such guarantee, so a hung `disconnect()` orphans
  the thread/loop/peer-connections and restart storms leak (Fold-D makes the leak
  *observable*, not *prevented*). **New default: `livekit://` is a first-class scheme
  whose `create_*` owns a SUPERVISED OUT-OF-PROCESS helper/sidecar**, speaking frames
  over the already-solved `zmq://`/unix path. First-class discoverability +
  EC-observability live at the **URL + registration** layer; process isolation is an
  impl choice that lets the OS reap what Python can't. In-process `Room` demoted to an
  optional fast-path, gated on a written **restart-storm acceptance test** (after N×
  create→traffic→destroy, thread/fd/native counts flat, OR the next `create_*`
  fail-closes until the prior bridge is reaped/the process recycled).
- **[FATAL unstated — Tesla] Write the duplex invariant.** camera→app + app→actuation
  on one `channel_id` = two `Room.connect` with one island identity = LiveKit
  last-session-wins (one room dies, mis-read as link death by Fold-A). Invariant:
  **one Room per (process, channel_id, island identity), shared by source+target** —
  OR **two distinct island identities** (a dedicated robot principal vs the human).
  Which → the identity model below.
- **[Identity — Kelvin+Tesla] A dedicated MACHINE principal, not the operator's
  session on the host.** Product rule = `DM(robot_user, human)`. A human token on the
  robot means phone+robot can't co-exist in the room (same LiveKit identity). The
  refresh-token storage/revocation/recovery lifecycle for that machine principal is a
  real sub-design, not a "provisioning step."
- **[Token boundary — Tesla+Carnot] Close the `island_url` hole; capability-scope the
  token.** `island_url` from pipeline config is the `scheme_http` SSRF hole relocated —
  a malicious base URL harvests the session bearer. Require: **allowlisted island
  origin, no free-form base URL from untrusted shares, session token never sent to a
  non-island host** (pin/validate like `scheme_http`). And the token must be
  **audience/capability-scoped to video-token minting**, not a full user session that
  also authorizes other island API actions.
- **[Decouple — Carnot+Tesla] Transport auth ≠ chat topology.** A reusable
  `aiko_services` transport must not REQUIRE a DM channel model to exist. Least
  privilege = an **explicit short-lived room capability (publish/subscribe bits)**, not
  "whatever DM means this quarter." DM-only/2-party is named a **temporary island
  policy**, not a DataScheme security theorem; a machine-room mint path must not wait on
  chat features (#2731).
- **[Dual-mode — Carnot+Tesla] Direct-mint becomes a SEPARATE scheme `livekit-local://`.**
  Keeping island-auth + `LIVEKIT_*` direct-mint behind one `livekit://` is
  config-dependent security operators misread; a flag typo must not silently select the
  master-secret path. Mutually exclusive by scheme name / package extra.
- **[Cross-thread media — Tesla] Buffer ownership + call-after-destroy gate.** Deep-COPY
  frames into the queue before `put` (the SDK recycles native buffers after the async
  iterator advances → use-after-recycle tearing that a pixel-identity test won't catch);
  a **generation counter / "closed" gate on the SYNC `capture_frame` path** so publish
  can't run after `VideoSource` teardown begins (Fold-B closed create-vs-first-frame but
  not destroy-vs-in-flight). Treat non-contiguous/wrong-dtype publish as ERROR.
- **[Runtime creds — Tesla] The mint chain is a lifecycle, not a connect-time check.**
  access-token → video-token → LiveKit JWT is a chain of expiries; the robot loop runs
  hours. Re-mint on every connect/reconnect; define behavior when mint succeeds but the
  room JWT expires mid-session — else Fold-A's disconnect path becomes an auth incident
  mislabeled as network. (Pulls reconnect/creds forward from step 3 into the spine.)
- **[Also folded, lower severity]** DataSource **track selection** must be explicit
  (a DM room accrues multiple tracks over time → wrong-video risk); backpressure must
  reach the **SFU/decoder** (subscription pause / adaptive quality), not only the Python
  queue.

**Re-cast build order (supersedes the one below):** 1 = the out-of-process helper +
`zmq://` frame bridge + island-endpoint capability-token fetch (allowlisted origin) +
the restart-storm acceptance test, publish direction, dedicated robot principal. 2 =
subscribe + track selection. 3 = in-process fast-path ONLY if step-1's isolation proves
insufficient AND the restart-storm test passes. Reconnect/creds-lifecycle is folded into
step 1, not deferred.

**This re-cast is UN-STRUCK** — per the crucible, a substantial post-Temper recast is
itself un-tempered; the four load-bearing changes (out-of-process default, duplex
invariant, island_url boundary, machine-principal identity) need a **round-2 strike**
before this is "battle-tested" enough to hand to Andy as build-ready. The author's 6
claims + Fold A–F remain the build checklist, not the ceiling.

---

_Original pre-Temper design below (provenance; the Shape/build-order are superseded by
the re-cast above)._

## Problem

aiko streams natively over `zmq://` / `rtsp://` / gstreamer — machine-to-machine, no browser
reach. LiveKit reaches browsers/phones (WebRTC/SFU) but lives *beside* the aiko fabric, not
in it. There is no way today to route media between an aiko pipeline and a LiveKit room, so a
robot's camera can't flow through an aiko inference pipeline to the app, and the app's video
can't feed an aiko pipeline. We want LiveKit to become **one more pluggable aiko transport**.

## Shape

A single `DataSchemeLiveKit(aiko.DataScheme)` registered as `add_data_scheme("livekit", ...)`,
implementing both directions, backed by one `_LiveKitBridge` helper.

### URL + parameters
- URL: **`livekit://<channel_id>`** — the room is an island *channel*, nothing else in the URL.
- Pipeline parameters:
  - `island_url` (e.g. `https://chat.enspyr.co`) — the token authority.
  - `island_token` — a **user session bearer token** the pipeline element holds (env
    `AIKO_ISLAND_TOKEN` or a share). NOT SFU creds.
  - `width`/`height`, `data_batch_size`, `queue_max` (backpressure), `rate`.

### The token model (the one atypical, thesis-serving element)
The scheme **never takes an SFU URL or a raw token in its config.** It calls
`POST {island_url}/v1/channels/{channel_id}/video-token` with `Authorization: Bearer
{island_token}` and uses the returned `{token, url}` verbatim for `Room.connect(url, token)`.

Consequences (all *by construction*, not by guard):
- **The SFU URL comes from the trusted island**, so a malicious pipeline config can't redirect
  a token to a rogue SFU (closes the token-exfil analog of scheme_http's SSRF hole).
- **The island enforces authorization** — DM-only, exactly-2-party, block gate,
  gateway_id-namespaced room+identity — so the pipeline inherits sovereign, DM-scoped,
  block-aware media access for free.
- **The bridge holds only a user session token** (blast radius = that one identity's DM
  rooms), never the SFU master secret.

### The async bridge (core mechanism — generalizes `scheme_zmq`)
`_LiveKitBridge` owns a **daemon thread running a dedicated asyncio loop** + the `Room`.

- **DataTarget (publish):** on the loop — connect, create `rtc.VideoSource(w,h)`, publish a
  track. `process_frame(stream, images)` (pipeline thread) calls
  `src.capture_frame(rtc.VideoFrame(w, h, RGB24, images[0].tobytes()))` — `capture_frame` is
  synchronous + thread-safe (verified this session), so no cross-thread await needed.
- **DataSource (subscribe):** on the loop — connect (`auto_subscribe`), on `track_subscribed`
  iterate `rtc.VideoStream(track)`, convert each frame to numpy RGB, `queue.put_nowait` with
  **drop-oldest on overflow** (bounded `queue_max`). `frame_generator` drains the queue
  (`NO_FRAME` when empty) — byte-for-byte the `scheme_zmq` pattern.
- **Teardown (`destroy_sources/targets`):** set a stop `Event`, `loop.call_soon_threadsafe`
  to `await room.disconnect()`, `thread.join(timeout=BOUNDED)`, close the loop. Mirrors
  scheme_zmq's `terminate` flag + bounded socket timeout so a stream restart never leaks a
  loop/thread.

### Frame adapter (numpy ↔ VideoFrame)
- Publish: `rtc.VideoFrame(w, h, RGB24, images[0].tobytes())` — zero conversion (RGB24 = packed
  3-byte RGB = contiguous HxWx3 uint8). *Assumes no stride/row-padding mismatch — see falsify #4.*
- Subscribe: `np.frombuffer(frame.convert(RGB24).data, np.uint8).reshape(h, w, 3)`.

## Build order (core-first, each step independently useful)

1. **DataTarget publish + island-endpoint token model** — the whole spine in one direction:
   `_LiveKitBridge` (publish), island token fetch, frame adapter, bounded teardown.
   *Independently useful:* robot camera → aiko pipeline → app watches. Demo-able alone.
2. **DataSource subscribe** — mirror direction, reusing bridge + token machinery. LiveKit track
   → aiko numpy frames. *Independently useful:* server-side inference on an app/robot track.
3. **Hardening** — bounded-backoff reconnect (lift AITW `livekit_service.dart` patterns),
   backpressure tuning, `_disposed`-guard-equivalent after each await, audio + data-channel
   tracks, Aiko Dashboard metrics.

## Blast-radius + consent spine (cage before monster)

- **Owner:** the bridge process (robot host, or island-local).
- **Injection surface & closures:** SFU URL — *closed by construction* (island-supplied).
  Token — *least-privilege* user session, not master secret. Room — *island-namespaced +
  DM-scoped*.
- **Throttle:** already present — the island `video_token` endpoint is per-IP rate-limited;
  the bridge inherits it. Token TTL is short (connect-time check).
- **Cross-repo consent:** `aiko_services` is Andy's. Path = fork → cage-match → upstream PR
  (the task-#30 `http` DataScheme precedent), never a direct push. **This crucible tempers the
  DESIGN; the built code still needs its own `/cage-match` (trust boundary: media + tokens)
  before any merge.**

## Claims to falsify (for the adversary)

1. **[PRIMARY]** The `scheme_zmq` daemon-thread+queue pattern generalizes to an asyncio `Room`
   loop with **leak-free teardown across stream restarts** (no orphaned loop/thread/socket).
2. `capture_frame` called from the pipeline thread, while the `Room`/`VideoSource` live on the
   bridge loop thread, is safe (native cross-thread enqueue) under sustained load.
3. **The island-endpoint token model is viable for a robot.** It requires the robot to hold a
   *user session token* — but the island is **passkey-primary and `/register` is force-closed
   in prod**. Is there any way for a headless robot to obtain a session token today? If not,
   this model has an unbuilt prerequisite (a service-credential path on the island). **This is
   the design's softest joint.**
4. `images[0].tobytes()` matches `rtc.VideoFrame(RGB24)` stride/row-alignment (no padding gotcha
   that corrupts every frame).
5. **DM-only + exactly-2-party** (the endpoint's hard constraint) fits the robot topology — a
   robot↔app pair must be a 2-party DM channel; a robot publishing to a *group* room is refused
   until selective subscription (#2731). Does the intended use fit 2-party?
6. Drop-oldest backpressure is acceptable for the use case (inference tolerates loss; a
   recording/archival target would silently lose frames — wrong for that consumer).

## Rejected alternatives

- **Direct `LIVEKIT_*` mint in the bridge** — simplest (no session-token plumbing), but puts
  the **SFU master secret** on the bridge host = mesh-wide room-forgery blast radius. *Rejected
  as default; retained as an escape hatch for an **island-local** bridge only (where the creds
  already reside).*
- **Out-of-process LiveKit publisher fed over `zmq://`** — aiko pushes frames over existing
  `zmq://` to a small standalone livekit-rtc process; no asyncio inside the pipeline. *Rejected
  as default (extra hop/process/failure-mode) but this is the **pressure-release valve if
  falsify #1 fires** — it sidesteps asyncio-in-pipeline entirely.*
- **GStreamer `webrtcbin` instead of livekit-rtc** — stay in aiko's native gstreamer stack.
  *Rejected: webrtcbin↔LiveKit signaling is a large integration; livekit-rtc handles it and is
  proven. Revisit only if the Python SDK fails on the robot host.*
- **No aiko integration (status quo)** — app talks LiveKit directly, robot bespoke. *Rejected:
  media stays outside the fabric; no pipeline/inference reuse; no discoverability.*

## Open variables (enumerated, not silent)

- **Robot user-identity/auth** (Fold-resolved, was "BLOCKER-CANDIDATE"): a headless robot
  obtains a session token via **one-time provisioning** — register a (software-authenticator)
  passkey once at setup, store the durable **refresh token**, and call the island's existing
  `POST /v1/auth/refresh` for short-lived access tokens. Mechanism already exists on the island
  (passkey register + `/refresh` both in the served OpenAPI). So this is a *provisioning step*,
  not an unbuilt prerequisite; the **direct-mint escape hatch** covers an island-local bridge in
  the interim. Downgraded from blocker.
- Code home: `aiko_services` fork vs island-local element (Andy's call).
- Audio + data-channel scope (video-first; deferred to step 3).
- Reconnect/backoff policy (lift AITW; deferred to step 3).

## Fold (author self-pass — findings folded back in)

Struck the casting against my own hardest read before the cross-family strike. Six degenerate
states / stressed claims produced these **design deltas** (now binding on the build):

- **A. Dead-connection must not masquerade as idle.** A live DataSource whose room drops would
  otherwise return `NO_FRAME` forever — indistinguishable from "peer isn't publishing yet." The
  bridge MUST watch `room.on("disconnected")` and either reconnect (step 3) or surface
  `StreamEvent.ERROR`; **never silent NO_FRAME on a dead link.** (Mirrors AITW's token-error /
  `_disposed` discipline.)
- **B. Init-ordering race (publish side).** `create_targets` spawns the async connect and
  returns immediately, but `process_frame` may fire before the `VideoSource`/track exist →
  `capture_frame` on `None`. Fix: `create_targets` **blocks on a bounded "publishing-ready"
  Event** set by the loop before returning OKAY; on timeout it returns `ERROR` (fail closed —
  never OKAY on a half-open target). zmq doesn't have this gap because zmq connect is sync; the
  async connect is the new hazard.
- **C. Token-fetch errors fail closed with clear diagnostics.** Map island responses:
  `503`→"video not enabled", `404`→"channel not a 2-party DM, or blocked", `429`→"rate-limited"
  → all `StreamEvent.ERROR`, no retry-storm.
- **D. Teardown is join-with-bounded-timeout, and a leak is loud.** `destroy_*` sets a stop
  Event, disconnects on the loop, `thread.join(timeout=T)`, closes the loop. If join times out,
  **log loudly** — a leaked loop/thread across stream restarts is the PRIMARY falsifier and must
  never be silent. (This is the falsify-#1 acceptance bar.)
- **E. Frame-format claim gets a pixel round-trip test, not reasoning.** RGB24-vs-numpy stride
  (falsify #4) is verified by an external known-answer test: publish a known pattern, subscribe,
  assert pixels match (the self-referential-test-blindness discipline — don't trust a
  self-roundtrip's *self*-consistency; assert against a *known* pattern).
- **F. New claim to verify:** does `VideoSource.capture_frame` **block or drop** when the native
  buffer is full? If it blocks, a fast pipeline stalls the `process_frame` thread; the publish
  side then needs its own drop policy, symmetric with the subscribe queue.

**Escape-hatch tested against my own problem:** the out-of-process zmq-publisher (rejected
alt #2) *does* dissolve falsify #1 — but it is NOT a DataScheme, so it fails the actual goal
("LiveKit as a first-class aiko transport"). It stays the pressure-release valve if #1 fires,
not a replacement. In-process bridge remains the target; the `scheme_zmq` precedent is strong
evidence #1 holds.

**Fold did NOT re-grade the ore** — the candidate is fixed; these are craft fixes that raise
the floor so the cross-family strike lands on what only a different inductive bias can see.
