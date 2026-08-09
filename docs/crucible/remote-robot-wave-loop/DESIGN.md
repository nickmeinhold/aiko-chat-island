# CAST → FORGE — Remote-Robot Wave Loop (final, buildable)

**One human waves at a camera in location A; a robot in location B (remote network) waves back — safely, tomorrow.**

Design crucible · Forge movement (temper verdicts reconciled) · 2026-08-10 · scope: the robot loop ONLY (the social A/V path is settled and out of scope).

**Temper outcome:** latency-robustness → REQUEST_CHANGES · actuation-security → REQUEST_CHANGES · feasibility-vs-Andy → APPROVE_WITH_FIXES. All three agree the *architecture* is sound (sign-not-ACL, sender-side detect, thin verifying bridge, reuse `XGORobot`). Two lenses block on the *implementation claims*. Every MUST-FIX below is folded in; the design is now buildable but no longer "steps 1-5, done, no caveats."

---

## The architecture (survives all three lenses)

```
LOCATION A (network A)                 SFU (imagineering)              LOCATION B (network B, a Pi)
┌──────────────────────────┐                                   ┌────────────────────────────────┐
│ camera → WaveDetector     │                                   │  LiveKit-subscriber BRIDGE     │
│  (MANUAL BUTTON first)     │       wss://livekit               │   1. on(data_received)         │
│        │ wave=True         │       .imagineering.cc            │   2. VERIFY (fail-closed):     │
│        ▼                   │  ───────────────────────────▶    │      • Ed25519 sig, allowlist  │
│  commander signs envelope  │  DATA packet, seq++,              │      • seq > durable high-water│
│  {robot_id,"wave",SEQ,     │  destinationIdentities=[robot]    │      • deadline (tight, clocked)│
│   signed_at_ms, sig}       │                                   │   3. SINGLE-FLIGHT gate        │
│        │                   │      (TURN relays if              │      (servo_busy → DROP)       │
│  publishData → SFU         │       symmetric NAT)              │   4. CLOSED table {"wave"→15}  │
│  token: publish-data only  │                                   │      → MQTT (action wave)  ────┐│
│  (dedicated commander key) │                                   │   5. heartbeat/"waved" back ──┐││
└──────────────────────────┘                                    │        XGORobot Actor ◀────────┘│
   island mints BOTH tokens, server-derived identity, per-role  │        UN-MOCKED action()→servo15│
                                                                 │        MQTT broker = loopback   │
                                                                 └────────────────────────────────┘
```

**The trust boundary is the signature verified inside the bridge, NOT the room ACL.** The SFU only relays bytes; the signature survives a malicious participant. Room membership admits presence; the allowlisted commander signature authorizes the *physical action*.

---

## Ground truth — corrections folded from all three lenses

1. **The island LiveKit token code is COMMITTED and WIRED, not "uncommitted."** (feasibility lens overturns the Cast claim.) `git ls-files` returns `domain/livekit_tokens.py` + `rest/livekit.py`; `main.py:246,265` imports and `include_router(livekit_routes.router)`; tree clean. **Consequence: Cast's risk #4 ("build on an unmerged foundation") is MOOT** — the robot-token variant builds on merged code. *Residual: verify the deployed islands actually run this router before minting robot tokens against prod (repo ≠ runtime).*

2. **The replay/nonce guard is NET-NEW durable state, NOT a "~30-line clone."** (security lens.) `signing.py` is **carrier-only** — validates envelope *shape*, never checks a signature, no nonce, no consume. `is_fresh()` is **pure timestamp arithmetic**; its own docstring says a captured packet "can be replayed for up to `max_age_ms + skew_ms`" and that the monotonic store "**doesn't exist yet**." Only the `signing_bytes` *construction* is a clone. The **verify path + durable replay defense are genuinely net-new** and must be built and tested as such.

3. **The "already-built servo path" moves NOTHING — the hardware call is commented `# MOCK`.** (feasibility lens.) `xgo_robot.py:241-245`: even in the production impl, `self._xgo.action(...)` is commented out and the method just publishes the string `(action wave)`. `"wave": 15` exists (line 100) — but on a real robot this is a string-emitter, not a mover. And line 240: `# Blocks robot (all threads) until action is completed :(` — the real call is **synchronous, blocking, and never yet run on this loop**.

4. **`livekit-rtc` (Python) is uninstalled, is the entire cross-network hop, and the "proven" evidence is in the wrong languages.** AITW's `publishData` is **Dart**; Dreamfinder is **Node** (`@livekit/rtc-node`). The bridge needs the **Python** Rust-FFI wheel on an **aarch64 Raspberry Pi**, sharing an event loop with the aiko/MQTT Actor. This is the single longest real pole.

5. **The physical robot at B is assumed, never grounded** — whose XGO, where, who has hands on it during the demo window. `is_robot()` mocks on a laptop, so a green integration test on a stand-in proves the network+auth loop and **nothing about the servo**.

---

## Conflicts between lenses — surfaced, not silently tie-broken

**C1 — Reliable vs unreliable data channel.** Latency lens: `reliable:true` is *wrong* — a mid-wave B-side flap makes the SFU redeliver the whole stale backlog, so the robot waves the backlog minutes later; it wants **latest-wins-or-nothing**. Security lens: wants a **monotonic seq** for replay defense.
**Resolution (one mechanism serves both):** the commander stamps a **durable monotonic `seq`**; the bridge persists a **high-water mark to disk (atomic write)** and **drops any envelope with `seq ≤ high-water`**. This is clock-independent replay defense *and* latest-wins staleness rejection *and* survives bridge restart — it satisfies both lenses at once. Transport choice then becomes secondary: keep `reliable:true` (simplest, and the seq guard neutralizes the backlog-redelivery risk), and RED-prove the flap (drop B mid-burst, assert no backlog replay).

**C2 — Freshness window vs cross-host clock skew.** Both latency and security lenses reject the inherited `DEFAULT_MAX_AGE_MS = 5 min` on a servo. Latency wants a tight ~1500-2000ms deadline re-checked at dequeue. Security warns that between two uncoordinated hosts the clock offset is seconds-to-minutes, so a tight wall-clock window **breaks in both directions** (B trails A → robot never waves, fail-closed silent no-op; B leads A → replays live longer).
**Resolution:** the **`seq` high-water (C1) is the primary, clock-independent replay defense** — it does not depend on clock agreement. The **tight deadline is a separate *timeliness* gate**, and it is only trustworthy under asserted clock discipline: **require NTP on both hosts, assert `|offset| < 250ms` at bridge start, and fail VISIBLY** (`log: "packet rejected as stale — check clocks"`) so a clock-skew reject is diagnosable at the demo, never a silent no-op. Pass an explicit `max_age_ms≈2000`, never inherit the 5-min default, and **re-check the deadline at dequeue** (immediately before actuation), so a packet that aged in the mailbox is dropped, not fired.

No other cross-lens conflicts — the three axes are otherwise orthogonal (authz shape, physical-loop robustness, feasibility).

---

## Authorization + robustness mechanism (the physical-safety crux, hardened)

Layered, fail-closed. The **signature is load-bearing**; the token grant is defense-in-depth. Ordered as the bridge must check them:

1. **Ed25519 signature** valid under a **single allowlisted commander pubkey** (the robot ships with the ONE authorized key — never "anyone in the room"). New `ACTUATE_DOMAIN_TAG = "aikochat:actuate:v1:EdDSA"`; `actuation_signing_bytes({robot_id, command, seq, signed_at_ms})` clones the length-prefixed domain-tagged construction from `reaction_signing_bytes`. **The verify helper is net-new** (see ground-truth #2). Unit-test with an **external known-answer vector**, not a self-roundtrip (a self-consistent codec can be self-consistently wrong).
2. **Durable monotonic `seq` guard** (C1) — replay + latest-wins + restart-safe.
3. **Tight timeliness deadline** (C2) under asserted NTP discipline, re-checked at dequeue.
4. **Single-flight servo gate** — a `servo_busy` cooldown = servo-duration + margin. While busy → **DROP** (never queue): the Actor mailbox is an **unbounded FIFO `queue.Queue()`** and the servo **blocks all threads**, so the bridge is the only place that can throttle a device it cannot preempt. Coalesce a burst within ~500ms into ONE actuation.
5. **Closed command table** (security B2) — the bridge maps the verified event through a **constant table `{"wave" → literal (action 15)}`** and **NEVER string-interpolates any envelope field into the MQTT S-expr.** Reflection dispatch (`actor.py Message.invoke()` → `__getattribute__(command)`) otherwise means one valid signature could drive `move`/`arm`/`terminate`. Test: a commander-signed envelope with `command="terminate"` must dispatch nothing but the fixed wave.
6. **MQTT broker loopback-only** (security B5) — asserted and **checked at bridge start**, not risk-list prose. The open Actor command surface is safe *only* because the broker is robot-local; if it binds a network-B-reachable interface, any LAN host actuates with no signature at all.
7. **Token grant scoping** (defense-in-depth) — robot joins its own room `robot-<id>` with a **`canSubscribe`-only** token (`canPublish=False, canPublishData=False`), grant assigned server-side by role, never from the request body (I5). Commander gets a `canPublishData` token. `destinationIdentities=[robot]` targets the packet (sender-cooperative, not a boundary on its own — which is why layer 1 exists).
8. **Closed loop / liveness** (latency B4) — the bridge publishes a **"waved"/heartbeat** data event back into the room so A (and a human operator) can distinguish *success* from *B-offline / stuck servo* **before and during** the demo. A fire-and-forget open loop makes silent bridge crash indistinguishable from success.

**Commander-key custody (named tradeoff, security B4).** For the demo the commander Ed25519 key lives at A and the robot allowlists that single pubkey — zero island-side signing change. **Two conditions attached:** (a) run the signer as a **separate process from the CV pipeline** (YOLO-pose decodes untrusted camera frames — highest RCE surface; do not co-locate it with the one key that moves the robot); (b) give the allowlist entry an **expiry**. *Correct long-term posture (adopt right after the demo): island-signs* — an authenticated detector reports "wave" to the island, the island signs with `island_signing_seed`, robot verifies against the pubkey it already fetches from `GET /v1/island`, gaining rotation/`key_version`/revocation for free. This binds actuation to the existing authorizer; deferring it past the demo is a **named, under-priced risk**, not a silent one.

---

## What is safe to build TONIGHT vs what blocks on Andy / hardware

**Safe tonight — pure software, island repo, no robot, no Andy:**
- Commit-verify the LiveKit token code is deployed; add the robot-scoped subscribe-only grant path.
- Build the `actuate` signing primitive, the **net-new verify helper**, and the **durable seq replay store** (mirror the OAuth `consumed`+guarded-UPDATE persistence pattern). Known-answer-vector test. **Cage-match it — trust-boundary-by-law.**
- Draft the bridge logic (verify → seq → deadline → single-flight → closed-table → MQTT) against a **mock SFU + mock MQTT** so the safety logic is RED-proven (forged packet rejected, replayed packet rejected across a simulated restart, stale-backlog dropped) before any real hardware exists.

**Blocks on Andy / physical hardware — cannot be closed tonight without the Pi:**
- **Step-0 go/no-go:** `livekit-rtc` installing on the actual aarch64 Pi and one packet round-tripping against the live SFU. If the wheel doesn't land, the loop is dead — you want that at hour zero.
- **Un-mocking `self._xgo.action(15)`** and testing the blocking xgolib call on the real servo — Andy owns the domain knowledge on the blocking-thread behavior.
- Grounding the robot: whose XGO, where, who has hands on it in the demo window. **If it's Andy's XGO at his site, Andy is on the critical path** for hardware access regardless of the code being library-reusable.

**Scope/expectation call for Nick (do not let it ship silently):** tomorrow's deliverable is **button→robot**, not **wave→robot**. Real wave detection (a net-new temporal-gesture aiko element — `YoloDetector` returns object boxes, not keypoints) is deliberately LAST, behind the same signed-event interface. `yolov8n-pose.pt` is zero-new-dep (`ultralytics` already imported), but the temporal heuristic is net-new and is the schedule long pole. **Confirm a manual trigger satisfies the demo narrative, or the schedule is wrong.**

---

## BUILD SEQUENCE for tomorrow

> **⭐ = CRITICAL PATH** (physical-safety boundary + the only true cross-network hop).
> Owner tags: **[island]** = aiko-chat-island repo · **[aiko]** = Andy's aiko_services + robot host · **[app]** = app tab · **[Nick]** = decision/grounding.

**0. ⭐ [aiko] STEP-0 GO/NO-GO — tonight, before anything robot-side.** Install `livekit-rtc` on the *actual robot Pi* (aarch64) and echo one data packet round-trip against the live SFU. Learn the real `publish_data`/`data_received` signature (positional vs `DataPacket`/options, `destination_identities` kwarg). **If the wheel doesn't install, the loop is dead — escalate immediately.** *Blocks on Pi access (Andy).*

1. **[Nick] Ground the robot + set expectation.** Whose/where/who-has-hands; put "Andy available for hardware" on the critical path if remote. Confirm **button→robot** satisfies the demo. *Do first, in parallel with 0.*

2. **[island] Commit-verify + robot-scoped token.** Confirm the deployed islands run `livekit_routes.router`; add a `canSubscribe`-only token path for `identity="robot-<id>"` in `room="robot-<id>"`, grant assigned server-side (I5). *Safe tonight.*

3. **⭐ [island] Actuate signer + verify + durable replay store.** `ACTUATE_DOMAIN_TAG`, `actuation_signing_bytes`, net-new `verify_actuation(payload, allowed_pubkeys)`, durable monotonic-`seq` high-water (OAuth `consumed` pattern, atomic disk persist). Explicit `max_age_ms≈2000`. **External known-answer vector test.** **Cage-match — trust-boundary-by-law.** *Safe tonight.*

4. **⭐ [aiko] Robot-host bridge.** `livekit-rtc` join (subscribe-only token) → `data_received` → verify sig+allowlist → seq>high-water → deadline (NTP-asserted) → single-flight `servo_busy` DROP-not-queue → **closed table `{"wave"→(action 15)}`** → MQTT `topic_in`. Assert **MQTT broker loopback-only** at start. Publish **heartbeat/"waved"** back. Self-echo/sender guard from Dreamfinder. *Blocks on step 0.*

5. **⭐ [aiko] Un-mock actuation.** Uncomment `self._xgo.action(...)`; test the **blocking** xgolib call on the real servo. Andy owns xgolib quirks. *Blocks on hardware.*

6. **⭐ [aiko] Commander at A with MANUAL BUTTON.** Keypress signs the envelope (seq++), `publishData(reliable=True, destinationIdentities=[robot])`. Commander key in a **process separate from any CV**, allowlist entry with expiry. *Proves the whole network+auth+servo loop without CV risk.*

7. **⭐ [aiko+island] Integration + RED-prove the safety assertions.** Button at A → SFU(+TURN) → bridge → real servo waves. **Measure** real dequeue-to-servo latency (don't assert). Assert, RED: (a) forged/unsigned packet from a 2nd participant → **rejected**; (b) exact replayed packet, incl. **across a bridge restart** → **rejected**; (c) drop B 10s mid-burst → robot does **NOT** replay the backlog; (d) `command="terminate"` envelope → **only** wave dispatches; (e) clock-skew reject logs visibly. *Blocks on steps 4-6.*

**Demo-tomorrow minimum = steps 0-7 (button→wave-back).**

8. **[aiko] Real `WaveDetector`** — `yolov8n-pose.pt`, `wrist.y < shoulder.y` sustained + lateral oscillation, debounced; emits the discrete event step 6 signs. **Same-week follow-on**, drops in behind the signed-event interface. *Not on the demo critical path.*

9. **[island] Island-signs migration** — move actuation authority off the detector host onto the island trust root (`GET /v1/island` pubkey, rotation/revocation). *Post-demo; closes the named key-custody tradeoff.*

**Critical path:** 0 → 3 → 4 → 5 → 6 → 7. Steps 2 and 3 are the only work that fully closes tonight; everything downstream is gated on the Pi (step 0) clearing and on Andy for hardware (steps 4-5). If step 0 fails to install the aarch64 wheel, the entire loop is blocked and the demo scope must be renegotiated with Nick that night, not on demo morning.

---

# Temper verdicts (raw)

## Lens: latency-robustness

Verified against source. My lens is latency + robustness only, and I found where the physical loop breaks. Evidence is concrete.

---

# TEMPER VERDICT (latency + robustness lens): **REQUEST_CHANGES**

The trust-boundary design is sound (not my axis). But on the physical-loop-robustness axis this design ships a robot that **double-waves, waves minutes late, and replays a stale backlog after any network flap** — and the demo has no way to tell success from silent failure. Two blocking defects, both grounded in the real code.

## BLOCKING-1 — The servo BLOCKS all threads, the mailbox is an UNBOUNDED FIFO, and nothing coalesces. This is a guaranteed N-actuation engine.

`aiko_services/.../xgo_robot.py:240`, verbatim:
```
# Review "xgolib": Blocks robot (all threads) until action is completed :(
def action(self, action_type):
```
plus the file-header TODO (`:30`): *"Determine if xgolib can be made asynchronous (non-blocking)?"* — it is **synchronous and blocking**. A wave is a multi-second physical move that freezes the whole Actor (battery monitor, video loop, everything).

The Actor mailbox is `queue.Queue()` — **unbounded FIFO, no maxsize** (`actor.py:239`, `mailbox_put` path). The design's nonce guard rejects **replay of the same packet**, but a real wave detector emits *distinct-nonce* events (debounce bounce, sustained-gesture re-fire, a person waving twice). Every distinct-nonce packet is admitted and **queued behind the blocking servo**. Result: one human wave → 3 signed events → robot waves 3 times, each ~2-3s, serially, long after the human is gone. There is no edge-trigger, no single-flight, no "servo busy → drop." Double/N-actuation on a physical device is a real failure and it's the *default* behavior here.

## BLOCKING-2 — The design reuses the 5-minute `is_fresh` window for a real-time physical actuation. A stale packet fires the servo minutes late.

`island_identity.py:96`: `DEFAULT_MAX_AGE_MS = 5 * 60 * 1000` (5 minutes). `is_fresh` bounds **replay age**, NOT **delivery latency** — the docstring is explicit it "VERIFIES NOTHING" about timing beyond recency. For a message or an identity manifest, 5 minutes is fine. For a servo it is catastrophic: a packet that sat 90s in a reconnect buffer or the mailbox behind a blocking wave still passes `is_fresh` and **actuates**. The robot waves back to a wave that happened minutes ago. The design conflates "not-replayed" with "still-timely" — wrong window class for a physical edge event. And freshness is checked at *receive*, never re-checked at *dequeue*, so a packet that aged in the queue is never re-evaluated.

## BLOCKING-3 — `reliable: true` is the WRONG channel for a physical edge trigger; a flap redelivers the whole stale burst.

AITW confirms the pattern is reliable (`map_sync_service.dart:569,683,701` `reliable: true`). The design's rationale — *"reliable:true never drops a wave"* — inverts the actual risk. Reliable/ordered doesn't just never-drop; it also **never-discards-stale**: on a mid-wave B-side flap, the SFU buffers the reliable stream and **redelivers the entire backlog on reconnect** → the bridge queues all of it → the robot waves the whole backlog minutes later. For a physical actuator you want **latest-wins-or-nothing**, the exact opposite of reliable-ordered. Plus a second stale-redelivery queue hides in the bridge→robot MQTT hop (persistent-session QoS redelivers on reconnect too) — two serial buffering queues, both feeding a blocking servo.

## BLOCKING-4 — The loop is open: no ACK, no liveness. B-offline and success look identical from A.

There is no return path. When the bridge is down or the servo is stuck, the commander at A keeps fire-and-forget `publishData`-ing into the void and never learns the wave didn't land. For a demo whose entire deliverable is *the visible wave-back*, a silent bridge crash is indistinguishable from success on the A side until someone looks at the robot. No heartbeat means "is B even alive?" is unanswerable before the demo starts.

---

## MUST-FIX (all on the robustness/latency axis; do before the demo, not after)

1. **Actuation deadline, not manifest freshness.** Pass an EXPLICIT tight `max_age_ms` (~1500-2000ms), never inherit `DEFAULT_MAX_AGE_MS`. And **re-check freshness at dequeue** (immediately before `action("wave")`), so a packet that aged in the mailbox is dropped, not fired.
2. **Single-flight + edge-trigger at the bridge (the coalescing point, BEFORE MQTT).** Hold a `servo_busy` flag / cooldown = servo-duration + margin. While busy → **drop** incoming waves, don't queue. Coalesce a burst within ~500ms into ONE actuation. The bridge must be the throttle because the Actor mailbox downstream is unbounded FIFO and the servo blocks — you cannot let a queue you can't drain feed a device you can't preempt.
3. **Latest-wins transport semantics.** Either switch the trigger to UNRELIABLE (lossy) data, OR keep reliable but stamp a monotonic seq and have the bridge drop anything whose seq ≤ highest-seen OR whose deadline passed. RED-prove the flap: drop B for 10s mid-burst in step 5 and assert the robot does **not** replay the backlog — the design's step 5 tests a *forged* packet but never tests the *stale-backlog* case, which is the likelier demo failure.
4. **Close the loop with a liveness signal.** Bridge publishes a "waved"/heartbeat data event back so A (and a human operator) can see the loop closed and detect B-offline BEFORE the demo. Minimum viable: a bridge heartbeat into the room.
5. **Measure, don't assert, the real dequeue-to-servo latency** end-to-end (network is the design's named risk #5, but the serial blocking queue is the bigger latency source and is currently unmeasured).

Fix 1 + 2 are the two that turn "physical robot that double-waves and lags minutes" into "waves once, promptly, or not at all." Without them, ship-ready is a mirage — the loop passes a happy-path integration test and breaks on the first debounce bounce or Wi-Fi hiccup, which at a live demo is a certainty, not a tail risk.

---

## Lens: actuation-security

# TEMPER VERDICT: REQUEST_CHANGES

The design's *framing* of the boundary is right (signature authenticates, room ACL does not; fail-closed on a physical actuator). But it asserts safety machinery that **does not exist in the repo it claims to clone**, and it leaves the reflection-dispatch attack surface wide open. Two of these are demo-blocking; the robot moves on a replayed or repurposed packet as designed.

## BLOCKING ISSUES (with evidence)

### B1 — The replay guard is claimed as "reuse" but is UNBUILT and the cloned module says so explicitly. (critical)
The design (§Authz layer 1, build step 2) states: *"monotonic nonce/seq — kills in-window replay"* and calls the whole thing a *"~30-line mechanical clone of the reaction signer."* Ground truth:
- `signing.py` (the reaction/message signer) is **carrier-only, no verification, no nonce, no consume** — it validates envelope *shape* and never checks a signature, let alone single-use.
- The only freshness primitive, `island_identity.is_fresh()`, is **pure timestamp arithmetic** (`return -skew_ms <= age <= max_age_ms`). Its own docstring is the falsifier: *"a captured manifest can be replayed for up to `max_age_ms + skew_ms`… strict epoch monotonicity that kills it permanently is a clean **future v3 when the A4 high-water store lands** — that store … **doesn't exist yet**."*
- The only single-use nonce stores in the codebase are DB-backed OAuth/social nonces (`models.py` `consumed`+`expires_at`, guarded UPDATE). Nothing for actuation.

So the replay defense is 100% net-new **durable, atomic, per-commander state** that the bridge must hold — not a clone of anything. And it conflates two incompatible mechanisms: a **nonce set** (unbounded memory, must persist across bridge restart or every past packet re-fires) vs a **monotonic seq** (a bridge that keeps its high-water mark only in memory resets to 0 on restart → every captured packet replays; and legit repeated waves require the counter to advance, which the design never specifies who owns). **As written, a captured "wave" packet re-fires the servo — for up to the freshness window — and survives a bridge restart. On a physical device that is the whole game.**

### B2 — Reflection dispatch means a valid signature can drive the ENTIRE motion API, not "wave". (critical)
The design shows the bridge publishing `(action wave)` and step 6 says future gestures "drop in behind the same signed-event interface." But `actor.py` `Message.invoke()` does `self.target_object.__getattribute__(self.command)` on the **command name parsed straight off the bus** — no allowlist. `XGORobot` exposes `terminate`, `move`, `arm`, `translation`, `turn`, `stop`, `reset`, `claw`, plus the 20-entry `ACTIONS` table. The instant the bridge templates the envelope's `command` (or arguments) into the MQTT emit — which is the natural way to "support more gestures" — a single valid commander-signed envelope authorizes **arbitrary physical actuation** (`arm 155 155`, `move`, `terminate`). The design never states the invariant that would close this. **MUST-FIX: the bridge maps a verified event through a CLOSED constant table `{"wave" → literal (action 15)}` and NEVER string-interpolates any envelope field into the bus S-expr.** This is the actual trust door and it's undefended in the doc.

### B3 — Cross-host clock skew breaks the freshness window in both directions. (blocking for the demo AND for safety)
`is_fresh` compares `signed_at_ms` (host A's clock, network A) against `now_ms` (robot host B's clock, network B) — two arbitrary machines, no NTP guarantee asserted. The design cites 150-250ms TURN latency as the constraint, but **the constraint is clock offset**, which between uncoordinated hosts is seconds-to-minutes. If B's clock trails A → every fresh packet is rejected → **the robot never waves → demo fails, fail-closed**. If B's clock leads A → replays live *longer*. And the default the design inherits is `DEFAULT_MAX_AGE_MS = 5 min` — five minutes of replay validity on a servo, and `is_fresh`'s own docstring warns the semantic ceiling is the caller's to impose (it only type-bounds to ~1<<62 ms). MUST-FIX: require NTP on both hosts and assert offset at bridge start; set the window to seconds not minutes; and note that without B1's nonce the window is the *entire* replay exposure.

### B4 — The commander key is outside the island trust root, unrevocable, and sits on the most-attackable host. (high)
The chosen demo option puts the sole physical-actuation key on the **detector host at A** and hardcodes its pubkey in the bridge allowlist. Consequences the design under-weights:
- The island is described as "THE AUTHORIZER," yet in this path it authorizes **nothing** about actuation — only the LiveKit room token. There is zero link between "who the island admitted to the room" and "who may move the robot."
- No revocation, no expiry, no `key_version` on the allowlist entry (contrast the LiveKit token's `nbf`/`exp`). Key compromise = permanent physical ownership until someone redeploys the robot. The island's own `signing_keys` / pubkey→account binding (#1816 PR B) — the exact machinery you'd want — is per memory **still unbuilt**.
- Step 6 puts a YOLO-pose CV pipeline (loading models, decoding untrusted camera frames) **on the same host as the sole actuation key**. That is the highest-RCE-surface box in the system holding the one credential that moves a physical robot. Backwards.

The "island-signs" alternative the design defers is the correct posture (key on the existing authorizer, robot verifies against the pubkey it already fetches from `GET /v1/island`). For a physical device, deferring it past the demo is a named-but-under-priced risk; at minimum the commander key must be isolated from the CV process.

### B5 — The robot-local MQTT broker isolation is a load-bearing invariant stated only as prose. (high)
Confirmed there is **no auth on the actor command surface** — anything that reaches `topic_in` dispatches. The bridge is the only thing between the public LiveKit room and that open bus. If the broker binds `0.0.0.0` (or any network-B-reachable interface), any host on B's LAN actuates the robot **with no signature at all**, bypassing the entire design. This must be an asserted deployment precondition (broker on loopback only, verified at bridge start), not risk-list prose.

## MUST-FIX (to reach APPROVE_WITH_FIXES)
1. **Build the replay store for real** and stop calling it a clone: a durable, atomic single-use nonce consume (mirror the OAuth `consumed`+guarded-UPDATE pattern, not `is_fresh`). Specify persistence across bridge restart. Pick nonce-set XOR monotonic-seq and name who advances it; if seq, it must be durable. RED-prove: replay the exact captured packet twice → second is rejected, including across a bridge restart.
2. **Pin the bridge emit to a closed constant table**; forbid any envelope field flowing into the MQTT S-expr. Test: a commander-signed envelope with `command="terminate"` (or `arm`) must NOT dispatch anything but the fixed wave.
3. **Assert clock discipline**: NTP on both hosts, offset checked at startup, freshness window in seconds. Make the reject-legit failure mode visible (log "packet rejected as stale, check clocks") so the demo doesn't silently no-op.
4. **Bind actuation to the island trust root or isolate the key**: prefer island-signs (robot verifies against `GET /v1/island` pubkey, gains rotation/`key_version`/revocation for free); if keeping the commander key for the demo, run the signer as a separate process from the CV pipeline and give the allowlist entry an expiry.
5. **Assert MQTT broker loopback-only** as a checked precondition of the bridge, not a note.
6. Keep the trust-boundary-by-law cage-match on the actuate signer AND on the bridge (steps 2+3) — and note the island LiveKit token code is still uncommitted, so the robot-token variant can't be built on an unmerged foundation.

**Net:** the architecture (sign-not-ACL, sender-side detect, thin verifying bridge, reuse XGORobot) is sound and worth building. But the doc's headline safety claim — "signed + fresh + nonce, mostly reuse" — rests on a replay store that the cloned module explicitly documents as **not yet existing**, and it leaves the reflection-dispatch surface (B2) and broker isolation (B5) as the real, undefended doors. Fix B1 and B2 before any packet reaches a real servo.

---

## Lens: feasibility-vs-andy

## TEMPER VERDICT: APPROVE_WITH_FIXES

The authorization design is sound and genuinely well-grounded — the signing-kit reuse and the "signature is the boundary, room ACL is not" framing survive scrutiny. But the feasibility claims on the **actuation half** and the **cross-network hop** are softer than the doc asserts, and one true blocker for "tomorrow" isn't even in the risk list. Buildable, honestly de-risked by the manual-trigger fallback — but not "steps 1-5, done tomorrow" without the fixes below.

---

### Blocking / must-fix findings (evidence, not vibes)

**1. The "already-built servo path" does NOT move a servo — the hardware call is commented out.**
`aiko_services/.../xgo_robot/xgo_robot.py:241-245`:
```python
def action(self, action_type):
    if action_type in ACTIONS:
# MOCK      self._xgo.action(ACTIONS[action_type])
        payload_out = f"(action {action_type})"
        aiko.message.publish(self.topic_out, payload_out)
```
`"wave": 15` exists (line 100) — that claim is TRUE. But even in the *production* `RobotImpl.action()`, `self._xgo.action(...)` is commented `# MOCK`. On the real robot this publishes the string `(action wave)` and moves **nothing**. The design bills this "EXISTS — reuse"; it's a string-emitter, not a mover. You must uncomment that line and drive the servo for the **first time ever** on demo day. Worse, the author's own comment at line 240 — `# Review "xgolib": Blocks robot (all threads) until action is completed :(` — means the actuation call is **blocking**, untested, interacting with the Actor event loop. MUST-FIX: budget explicit time to uncomment + test the real xgolib call on the physical robot before the demo; do not treat actuation as assembled.

**2. `livekit-rtc` Python is the entire cross-network hop, is NOT installed, and the "PROVEN" evidence is in the wrong languages.**
`import livekit` fails; there is no proven Python data-channel path anywhere in these repos. AITW's `publishData` is **Dart** (Flutter game tests) and Dreamfinder is **Node** (`@livekit/rtc-node`, in `enspyrco/infra`). The robot bridge needs the **Python** SDK — a *separate* Rust-FFI wheel that must (a) install on the robot's **aarch64 Raspberry Pi** (the `is_robot()` path imports `RPi`, `spidev`, `xgolib` — this is a Pi), (b) have its `data_received`/`publish_data` API learned from zero, (c) share an event loop with the aiko/MQTT Actor. The design flags this risk #1 but then still calls the demo same-day. Installing + learning + wiring an unproven native SDK on ARM is the single longest real pole. MUST-FIX: make "install `livekit-rtc` on the actual robot Pi and echo one round-trip packet against the live SFU" the **step-0 go/no-go gate**, before any other robot-side work — if the aarch64 wheel doesn't land tonight, the whole loop is dead and you want to know at hour zero, not hour ten.

**3. The physical robot at B is assumed and never grounded — it's the #1 unknown and it's not in the risk list.**
The premise ("a robot in B, remote network") silently requires: a physical XGO Mini, powered, at location B, on a Pi with Raspbian + `RPi.GPIO`/`spidev`/`xgolib`/`aiko_services` installed, with outbound WSS to imagineering. `is_robot()` (line 61) gates all of that; on a laptop stand-in it **mocks**, so step-5's "integration test passes" proves the network+auth loop but **nothing about the servo**. MUST-FIX: before committing to "tomorrow," answer three preflight questions out loud — *whose* robot, *where* physically, *who has hands on it* during the demo window. If it's Andy's XGO at his site, **Andy IS on the critical path** for hardware access and xgolib quirks (that blocking-thread comment is domain knowledge), regardless of the code being reusable-as-a-library.

**4. The "demo tomorrow" has no real wave detection — it's a human pressing a button.**
Dep claim is TRUE: `ultralytics 8.4.48` is installed, `yolov8n-pose.pt` auto-downloads, so pose is "zero new dep." But `yolo.py`'s `YoloDetector` returns object boxes (custom `yolov8n_robotdog.pt`), not keypoints; a temporal wave gesture is a **net-new element that does not exist**. The design honestly defers it to step 6 (last) — fine engineering, but it means the tomorrow deliverable is *button→robot*, not *wave→robot*. If the demo's whole story is "I wave, it waves back," a keypress at A guts the narrative. This is a **scope/expectation call for Nick**, surface it explicitly — don't let "steps 1-5 = the demo" quietly ship a button as the headline.

---

### Grounding corrections (design was wrong, mostly in its favor)

**5. The island LiveKit code is NOT "uncommitted/untracked" — it's committed, tracked, clean, AND wired.** `git ls-files` returns both files; `git status` clean; `main.py:246,265` imports and `include_router(livekit_routes.router)`. Better than the design believed — but it's a grounding miss in exactly the spot the doc congratulated itself ("I have accurate ground truth now"). Net effect: the robot-token variant builds on a *merged* foundation, so risk #4 in the doc ("build on unmerged foundation") is moot — good. Still: verify the deployed islands actually run this router before minting robot tokens against prod.

**6. Signing/authz half is solid — no feasibility attack lands.** `signing.py` confirms `DOMAIN_TAG`/`REACT_DOMAIN_TAG`/`reaction_signing_bytes`/`is_fresh`. An `ACTUATE_DOMAIN_TAG` clone is genuinely mechanical (~30 lines). The layered fail-closed model (signature enforced, grant defense-in-depth, dedicated robot room subscribe-only) is the right shape for a physical-device boundary. Keep the trust-boundary-by-law cage-match on it.

---

### MUST-FIX checklist before "tomorrow" is a credible claim
1. **Step-0 go/no-go:** install `livekit-rtc` on the *actual robot Pi* and prove one packet round-trips against the live SFU — tonight, before anything else.
2. **Ground the robot:** whose/where/who-has-hands; if it's remote and Andy's, put "Andy available for hardware" on the critical path.
3. **Un-mock actuation:** uncomment `self._xgo.action(...)`, test the blocking xgolib call on real hardware; do not count it as done from the mock.
4. **Set Nick's expectation:** tomorrow = manual-trigger→wave (button, not CV). Confirm that satisfies the demo, or the schedule is wrong.
5. Keep the signed-envelope + subscribe-only-room authz exactly as designed; cage-match the `actuate` tag with a known-answer vector (not self-roundtrip).

Approve the *design*; the physical-loop feasibility is contingent on findings 1-3 clearing tonight. The manual-trigger fallback is the correct de-risking and is why this is APPROVE_WITH_FIXES rather than REQUEST_CHANGES.

---

