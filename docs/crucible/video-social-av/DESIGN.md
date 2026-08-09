# Crucible (recovered) — Video streaming over the aiko backbone (social A/V)

**Provenance.** This doc is *recovered* from session `c0295f44` (2026-08-09/10), where
the crucible ran **Ore (candidate) + Heat (two parallel scout agents) + a Cast-shaped
synthesis**, then was interrupted by `/consolidate` before **Fold / Temper / Blade**
could run — the "jumped the gun" moment. The distilled conclusions were captured in
`reference_video_streaming_livekit_aiko_architecture.md`; this restores the fuller
Cast text so the social-A/V path has a durable design doc symmetric with
`docs/crucible/remote-robot-wave-loop/DESIGN.md`.

**Status of the movements:**
- ✅ **Ore** — candidate selected: "add video via aiko pipelines, robust via AITW."
- ✅ **Heat** — two scouts (Andy's `aiko_services` stack; AITW's LiveKit) + a productive collision.
- ✅ **Cast** — the architecture below.
- ⛔ **Fold / Temper** — never ran here. *Partially discharged since:* the token/identity
  trust boundary is now built (`feat/livekit-video-token`) and slated for its by-law
  cage-match; that cage-match IS the missing Temper on the island's half.
- ▶️ **Blade** — superseded by this session's decisions (social-first sequencing chosen;
  gateway token endpoint built; app-tab handoff filed as claude-tasks#2726).

---

## The keystone (why this collapses to one small piece)

aiko's **DataScheme** is a pluggable media-transport registry keyed by URL prefix
(`file://`, `zmq`, `rtsp`, `tty`, `colab`). **The bus never carries media bytes** — only
the control-plane *reference* (the scheme URL); the scheme's own transport moves the bytes
out-of-band, exactly how aiko already sends images. So "add video via pipelines + make it
robust via AITW" is not a mash-up — it's **native**: Andy's pipeline is the source/ML, a
new scheme is the ~20% of glue, LiveKit is the robust wire, and the island issues room
tokens (the same JWT shape it already mints). Streaming raw video over the island's WSS
would be the naive/wrong move — media rides LiveKit's SFU; the island only brokers access.
That is what makes it robust **by construction**.

## The productive collision (settled the core fork)

- **Scout 1** (Andy's stack, never having seen AITW) guessed the bridge would be
  "aiko RTMP → **LiveKit Ingress**" — because `VideoStreamWriter` already has a working
  RTMP output mode.
- **Scout 2** (grounded in AITW) found AITW runs **no Ingress/Egress service** at all —
  its server-side audio (the **Dreamfinder** agent) publishes tracks *directly* via the
  LiveKit SDK (`AudioSource → LocalAudioTrack → publishTrack`).
- **Resolution:** the native path isn't Ingress — it's **mirror Dreamfinder's
  direct-publish pattern for VIDEO, in Python, inside a pipeline element**. `livekit.rtc`
  has `VideoSource`/`LocalVideoTrack`, the exact mirror of the Node `AudioSource`. Same
  proven topology, just video instead of audio, inside aiko's DataScheme registry.
  *(This is exactly the assumption the robot-loop crucible later hardened: `livekit-rtc`
  Python on the target host is the real long pole — GO/NO-GO it early.)*

## The architecture (~80% assembly, ~20% net-new)

| Layer | What it is | Status |
|---|---|---|
| **Source + ML** | aiko pipeline `webcam/RTSP → YoloDetector → ImageOverlay` | ✅ Andy's, working |
| **Egress bridge** | a `LiveKitVideoTarget` pipeline element: aiko numpy frame → `livekit.rtc.VideoFrame` → `VideoSource` → publish track | 🔨 **net-new, small** (Python mirror of Dreamfinder's audio-publish). *Not needed for social increment 1 — the phone publishes its own camera; this is for the robot-vision source.* |
| **Transport** | self-hosted LiveKit SFU `wss://livekit.imagineering.cc` (v1.11.0, TURN-configured, Redis, Caddy TLS) | ✅ **already deployed** |
| **Signaling / tokens** | island mints LiveKit room JWTs — same shape as its existing auth JWTs, secret stays server-side | ✅ **built this session** (`domain/livekit_tokens.py`, `rest/livekit.py`) |
| **Control-plane ref** | chat message carries a `livekit://room/track` reference; **bus never carries media** | ✅ DataScheme pattern exists (carriage TBD) |
| **Client** | aiko_chat_app subscribes via LiveKit Flutter SDK, reusing AITW's reconnect machinery | 🔨 net-new UI, proven patterns (handoff: claude-tasks#2726) |

**Robustness = literally lift AITW's patterns** (`room_session.dart` + the signed webhook):
bounded-backoff reconnect `[2s,4s,8s]` on the *terminal* `RoomDisconnectedEvent` (rebuild
the Room, don't patch it); `_disposed`-guard-after-every-await (a Carnot cage-match catch);
token-error-aborts-reconnect; TURN relay forced (`iceTransportPolicy: all`); signed-webhook
presence reaper with a freshness watermark. Copy, don't reinvent.

## Two gotchas that WILL bite if we cargo-cult AITW

1. **Do NOT copy AITW's `RoomOptions`.** AITW *disables* `adaptiveStream`/`dynacast`/
   `simulcast` on purpose — it renders remote video through the **Flame game canvas**, which
   never signals viewport demand, so the SFU stops forwarding after ~42 frames. A normal
   video feature renders through LiveKit's `VideoTrackRenderer`, which *does* signal
   demand → **re-enable all three.** AITW's defaults are the exact wrong ones for us, and
   their own comments admit it.
2. **Identity model differs.** AITW keys LiveKit identity on the **Firebase UID**; aiko has
   no Firebase → the island mints the room token keyed on the **sovereign aiko identity**.
   The one real token-layer adaptation, squarely island territory. *(Built: the token's
   participant identity is the authenticated user id, server-derived — I5.)*

## The honest cost: four repos, Andy on the critical path (for the ML source)

- `aiko_services` (**Andy's**) — the `LiveKitVideoTarget` element → collaboration/handoff.
  *Only needed for the robot-vision source, not social increment 1.*
- `aiko-chat-island` (**this repo**) — token endpoint (✅ built) + `livekit://` carriage.
- `aiko_chat_app` (**app tab**) — subscribe/publish UI + reused robustness (claude-tasks#2726).
- LiveKit infra (**imagineering**) — SFU exists; only token-minting config needed.

## The fork the Cast left for Nick (resolved this session)

> Primary use case: **human camera-to-viewers** (social video in chat) or the
> **YOLO/robot-vision annotated feed**? The *source* layer differs; everything downstream is
> identical.

**Resolved 2026-08-10: social-first.** Increment 1 = Nick+Robin A/V (phone publishes its own
camera; no `LiveKitVideoTarget` needed). Increment 2 = the robot-vision / remote-robot loop,
designed in `docs/crucible/remote-robot-wave-loop/DESIGN.md`.

## What still needs the fire (the un-run Temper)

- The **token grant model + auth boundary** — by-law cage-match on `feat/livekit-video-token`
  before non-demo traffic. This is the Temper the previous session never got to, on the
  island's half.
- **`livekit://` control-plane carriage** in the wire contract — not yet built.
- The **`LiveKitVideoTarget`** Python element — deferred with the robot-vision source; its
  real feasibility (aarch64 `livekit-rtc` wheel) is the robot-loop crucible's STEP 0.
