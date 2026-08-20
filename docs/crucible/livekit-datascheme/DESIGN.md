# DESIGN — `webrtc://` DataScheme (Cast)

Status: **RE-CAST round 2 after Temper round 2 — UN-STRUCK (needs a round-3 strike before build).**
Target repo: `aiko_services` (Andy Gelme's) via fork PR — NOT a unilateral commit.

**Renamed `livekit://` → `webrtc://`.** Andy, 2026-08-12: *"The DataScheme references the
transport, e.g https://, mqtt://, zmq://, webrtc://"*. LiveKit is our **implementation**, not
the transport. A scheme named after one vendor forecloses a second SFU and misdescribes the
layer. `livekit-local://` (direct master-key mint, dev/island-local only) keeps its name
because it genuinely IS LiveKit-specific.

---

# ROUND 2 RE-CAST — the authoritative design

Folds all eight flaws from [`TEMPER.md`](TEMPER.md) (round 2: Maxwell + Kelvin + Carnot +
Tesla, full 4-way, **unanimous that the round-1 default must invert**). Round 1's re-cast is
preserved below as provenance and is **superseded**.

**What changed, in one sentence:** round 1 chose a process boundary to contain a resource leak;
the spike measured that leak at **0.00 threads and 0.00 fds per cycle** and found the real
defect to be an unbounded *hang* — so the fix is a **clock, not a process**.

## D1 — In-process Room is the SPINE. Bounded teardown is the liveness invariant.

`webrtc://` create_sources owns a **daemon thread running a dedicated asyncio loop + the
`Room`**, in the pipeline process — the `scheme_zmq` shape, which is what the original Cast
proposed and round 1 overturned on a premise that has since been falsified.

**The liveness invariant, which replaces the process boundary as the mechanism:**

> No pipeline-lifecycle operation may ever wait on SFU liveness. Every await that can touch
> the network is raced against a deadline; on expiry the transport is **abandoned, not
> awaited**, and the Room is declared dead.

Concretely, `destroy_sources`:
1. sets the stop flag and **immediately stops accepting publish frames** (a generation gate —
   a capture after teardown begins is an ERROR, not a late frame);
2. issues `disconnect()` raced against deadline `T_disconnect` (default 5s);
3. on expiry **abandons the await**, logs a loud ERROR, and returns — never blocks;
4. joins the bridge thread against `T_join`; on expiry, logs loudly and marks the scheme
   **degraded**.

**Why abandoning is safe, and why this is now an evidenced claim rather than a hope:** the
spike's hostile arm A *is* exactly this operation — `wait_for(disconnect(), T)`, timeout fires,
Room abandoned, reference dropped — repeated 8× against a black-holed SFU, with **0.00 thread
and 0.00 fd growth per cycle**. Python cannot kill the stuck coroutine and this design stops
pretending it can; it makes the stuck coroutine *irrelevant to the pipeline's lifecycle*.

**Restart-storm policy (the degenerate state that must not silently pile up).** After a
timed-out teardown the abandoned Room may still hold an SFU seat until the server's own
participant timeout. So a subsequent `create_sources` for the same `(room, identity)` **fails
closed** while a prior bridge is unreaped, rather than stacking a second live Room behind a
stuck one — see D2, which this is the same invariant as.

## D2 — Duplex invariant, restated at the right granularity and given an owner

Round 1 wrote: *one Room per `(process, channel_id, island identity)`*. **`process` is deleted
from the key.** Maxwell and Tesla independently found that keying on `process` makes the
invariant trivially satisfiable while the harm it exists to prevent goes unmitigated: two
pipelines, or a restarted pipeline racing an unreaped predecessor, each hold one Room in one
process and still collide, and LiveKit's last-session-wins silently kills one.

> **INVARIANT: at most one live `Room.connect` per `(room, identity)`, HOST-WIDE.**

**Enforcement point (an invariant without one is a sentence, not a design):** a process-local
registry keyed by `(room, identity)` inside the scheme module, which D1's in-process spine
makes possible again — out-of-process there was no shared place to hold it. Source and target
for the same pair **share one Room object**, they do not open two.

Cross-process collision on one host is prevented by **identity allocation**, not by a lock:
one machine principal per bridge instance (D4). Two bridges ⇒ two identities ⇒ no collision by
construction. This is the coupling *removed* rather than guarded.

**Still untested.** LiveKit's last-session-wins behaviour is asserted, not measured. **Merge
blocker G3.**

## D3 — The island is the token authority; the token is capability-scoped

Unchanged in substance from round 1 and **unchallenged by any family** — but discharged by
nothing, so it is restated as a gate rather than a decision.

- The scheme **never** accepts an SFU URL or raw token in pipeline config. It calls the island
  for `{token, url}` and uses them verbatim.
- The island origin is **allowlisted operator/host config**, never a free-form base URL from a
  pipeline share — this is `scheme_http`'s SSRF hole relocated, and it closes the same way.
- The token is **audience/capability-scoped to room-token minting**, not a full user session
  that also authorizes other island API calls.
- The mint chain (access → video-token → LiveKit JWT) is a **lifecycle, not a connect-time
  check**: re-mint on every connect/reconnect, and define behaviour when the room JWT expires
  mid-session so Fold-A does not label an auth failure as network death.
- Direct master-key mint stays a **separate scheme** (`livekit-local://`), never a flag — a
  config typo must not silently select the master-secret path.

## D4 — A dedicated MACHINE principal, and its lifecycle is a sub-design, not a footnote

Round 1 downgraded this from blocker to "a provisioning step" on the strength of a
register-passkey-once-then-refresh flow. **That downgrade is withdrawn.** The path has never
been exercised headlessly, the island is passkey-primary with `/register` force-closed in prod,
and agent identity (#3096) is PR#136 — **open, not merged**.

Reinstated as an open dependency with a named surface: provision, store, rotate, revoke,
recover, and operator-transfer. A robot that cannot have its credential revoked is a robot you
cannot take off the network.

## D5 — Choose the host. Do not OR it. (NEW — round 2)

Round 1 said the bridge runs on "a robot host, **or** island-local". Those have **opposite
blast-radius polarity**, so one default cannot serve both:

| | pipeline/robot host | island gateway container |
|---|---|---|
| process death | **is the janitor** — Room dies with the pipeline | long-lived; nothing reaps |
| a hung await | affects that pipeline | can **mute chat**, which shares the box |
| an orphan | dies at next reboot | holds an SFU seat until someone notices |

**The call: the pipeline/robot host is the target for the DEFAULT in-process spine.** That is
where aiko pipelines actually run, it is the deployment whose fate-sharing makes D1 safe, and
it keeps a media transport out of the gateway that serves chat.

**Island-local is OUT OF SCOPE for v1.** If it is ever adopted it requires the D6 sidecar mode
plus an island-side participant janitor, because in a container the two failure modes above
stop being hypothetical. *(This is a design call, not a product ruling — flagged for Nick, who
owns whether media ever runs inside the gateway.)*

## D6 — The sidecar survives as an OPT-IN isolation mode, with a written contract

Not deleted — it has real uses (fault-isolating native crashes; keeping `livekit` out of a
host that must not import it). But it is **opt-in**, and it is **illegal without all four** of:

1. **Supervisor escalation:** SIGTERM → `T_term` grace → **SIGKILL** → loud on survival. Not a
   hardening detail: the spike measured the sidecar ignoring SIGTERM 8/8 with every child
   reaped by SIGKILL, so escalation is the entire mechanism by which the mode works.
2. **Lifetime bound to the parent:** a parent-death reaper (`getppid` watchdog, or
   `PR_SET_PDEATHSIG` / kqueue / cgroup). aiko's Pipeline installs **no SIGINT/SIGTERM
   handler**, so `destroy_sources` never runs on a hard kill and the child must be able to
   notice on its own that nobody owns it. **State change must precede any logging** — the
   spike's first watchdog failed because it printed to a pipe whose reader had just died, and
   `BrokenPipeError` killed the watchdog before it could stop anything.
3. **The child bounds its own `disconnect()`** — D1's invariant applies in whatever process
   awaits the SFU. A process boundary does not exempt anyone from the clock.
4. **No reconnect once the parent is gone.** A zombie that reconnects is a zombie that kicks
   the living, via D2's last-session-wins.

## D7 — Fidelity is a term of the URL contract, not an inherited property

The in-process spine yields **numpy frames natively**: no second codec, no transcode, and the
whole JPEG question disappears. That is now a reason for D1, not just a consequence of it.

Under the D6 sidecar, frames cross a process boundary and must be serialized. aiko's existing
convention (`image_io.ImageWriteZMQ` → `image_to_bytes` → `PIL.save(format="JPEG")`) puts a
**second lossy codec in series with VP8**. Measured tolerable (1.2ms/frame, ~6 KiB/frame,
pixel test still passes at 1.0 MAE) — but tolerable is a consumer-class judgement, and this
design must not make it silently on the consumer's behalf.

> **A `webrtc://` consumer chooses its fidelity; it never inherits it.** An explicit
> `fidelity` parameter (`lossless-frames` | `jpeg`) with `lossless-frames` as the default.

Inference tolerates a second codec. Measurement, archival, and anything a control loop acts on
do not. **Open question for Andy** (and the one that rests on a measurement rather than an
estimate): should `webrtc://` under zmq carry JPEG for compatibility with today's elements, or
does `aiko_services` want an opaque/raw media type so a bridged stream stays single-codec?

## Merge blockers — what is NOT discharged

The spike proved transport facts. It proved **nothing** about auth or identity: it mints from
the SFU `--dev` key.

- **G1 — Long soak before the in-process default is called sound.** Thousands of
  create/traffic/destroy cycles with hostile SFU pauses, tracking RSS *and* native threads/fds,
  plus proof that a successor stream can be created after a timed-out teardown. The
  unexplained **~0.4 MB/cycle RSS drift** is the one live reason to doubt D1 and 16 cycles do
  not settle it either way.
- **G2 — Publish direction** is unimplemented and unmeasured. `webrtc://` is DataSource-only.
- **G3 — The duplex collision (D2) is untested.** Measure LiveKit's same-identity
  double-connect behaviour before it drives identity architecture.
- **G4 — D3 and D4 exercised for real**: allowlisted origin validation, audience-scoped mint,
  refresh, mid-session expiry, machine-principal provisioning end to end on an island.
- **G5 — No production `webrtc://` may mint from an SFU master key.** `livekit-local://` only.

## Build order (supersedes both earlier orders)

1. **In-process subscribe + the D1 bounded-teardown/restart-storm contract**, with the hostile
   teardown and successor-creation tests as its acceptance gate. Useful alone: a room's video
   into an aiko pipeline.
2. **Publish direction** with the call-after-destroy generation gate (G2).
3. **Island capability auth + machine principal** (D3, D4 — G4/G5).
4. **The D6 sidecar mode, only if G1's soak or a real deployment constraint justifies it.**

## Status

**UN-STRUCK.** This round-2 re-cast has not itself faced a strike; per the crucible's own rule,
a substantial post-Temper recast is un-tempered until struck. Round 3 of a ≤3-round budget.

---

# ROUND 1 RE-CAST — SUPERSEDED (provenance)

_Kept for the record. Its D1 (out-of-process default) was **reversed** by round 2, and its
duplex invariant was **restated** with `process` removed from the key. Everything below is
historical._

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
