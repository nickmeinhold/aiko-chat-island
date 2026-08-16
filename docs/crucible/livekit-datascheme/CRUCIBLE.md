# 🜂 CRUCIBLE — `livekit://` DataScheme (LiveKit as a pluggable aiko streaming transport)

**Selected:** by Nick, explicitly (consent gate crossed at invocation). Session 2026-08-11.

## The pick

Make LiveKit a first-class **`aiko_services` `DataScheme`** — a URL scheme `livekit://`
that plugs into the existing pipeline model exactly like `zmq://`, `rtsp://`, `http://`:

- **DataSource** — subscribe a LiveKit track → emit **numpy RGB frames** into an aiko pipeline.
- **DataTarget** — consume aiko numpy frames → **publish** to a LiveKit room.

So a robot becomes `camera(rtsp://) → [inference] → livekit://<room>` and the app sees it;
the app becomes `livekit://<room> → actuation-pipeline` and drives the arm. Media stops
being a bolted-on side-stack and becomes a **first-class aiko stream** — discoverable,
service-authed, EC-observable like everything else in Andy's model.

## Why this thrills me AND what it changes

- **It dissolves a false either/or.** The prior framing was "LiveKit *vs* aiko streaming."
  This makes it "LiveKit *as* an aiko transport" — the browser/mobile WebRTC edge (which
  aiko cannot do) and the device/mesh fabric (which aiko does natively) meet at one seam,
  and that seam is 40 lines of adapter, not a new subsystem.
- **It unblocks the robot loop** (the "robot waves back" increment 2) on aiko-native rails,
  reusing the signed-actuation work (#123) already merged.
- **The tallest risk is already climbed.** This morning `livekit-rtc 1.1.14` published a
  video track and a second client received it (`kind=2`) against *both* live island SFUs.
  The Python publish/subscribe path is proven, not hypothetical.

## The spark (if true, drop everything)

**Both worlds already speak numpy.** An aiko video frame is a numpy RGB array
(`video_io.py`); a LiveKit frame is `rtc.VideoFrame(w, h, RGB24, bytes)`. The impedance
match is ~1 line each direction — so "bridge two media universes" collapses to "adapt a
frame-generator." If that holds, LiveKit-in-the-aiko-fabric is a weekend, not a quarter.

## The falsifier (what would prove this ore is slag)

**If the asyncio↔synchronous-pipeline bridge cannot be made clean** — i.e. running a
LiveKit `Room` event loop inside aiko's threaded `frame_generator`/`process_frame` model
requires either (a) forking the pipeline runtime, or (b) a teardown that leaks loops/threads
across stream restarts — then this is slag: the frame-format ease is a decoy and the real
cost is a runtime-integration rabbit hole. **Test:** does `scheme_zmq`'s existing
daemon-thread + `queue.Queue` + `terminate`-flag pattern generalize to an asyncio event
loop with a clean `destroy_sources/targets`? If yes, the ore is gold. If the loop lifecycle
fights the pipeline lifecycle, it's slag.

Secondary falsifier: **if the only safe token model forces the bridge to hold long-lived
`LIVEKIT_*` minting creds** (full SFU-forgery blast radius on a robot host), the security
cost may exceed the elegance — the design must find a least-privilege token path or accept
a named, owned tradeoff.
