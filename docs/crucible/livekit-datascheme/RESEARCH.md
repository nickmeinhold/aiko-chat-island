# RESEARCH — `livekit://` DataScheme (Heat)

Grounded against the real checkouts this session (not memory): `aiko_services`
(`/Users/nick/git/orgs/aiko/aiko_services`), `aiko-chat-island`, and a live
`livekit-rtc 1.1.14` test against both island SFUs.

## 1. The `DataScheme` contract (from `main/scheme.py`, `main/source_target.py`)

A `DataScheme` is registered in a global `LOOKUP` (`add_data_scheme("zmq", DataSchemeZMQ)`)
and dispatched by URL scheme (`parse_url_scheme("livekit://x") → "livekit"`). The interface a
scheme implements:

- `create_sources(stream, data_sources, frame_generator=None, use_create_frame=...)` →
  `(StreamEvent, dict)`. Sets up ingress, then calls
  `self.pipeline_element.create_frames(stream, frame_generator, rate=...)` to drive the
  pipeline pulling frames.
- `create_targets(stream, data_targets)` → `(StreamEvent, dict)`. Sets up egress; stashes the
  handle in `stream.variables[...]`. The owning `DataTarget.process_frame(stream, images)`
  then consumes.
- `frame_generator(stream, frame_id)` → `(StreamEvent, {"records": [...]})`. Called
  repeatedly. Returns `OKAY`+records, or **`NO_FRAME`** (nothing right now — poll again;
  used by a live source), or **`STOP`** (source exhausted — used by `http`, never by a live
  stream). Batches up to `data_batch_size` records per call.
- `destroy_sources(stream)` / `destroy_targets(stream)` — teardown.

`StreamEvent` values seen: `OKAY`, `NO_FRAME`, `STOP`, `ERROR`.

## 2. The async-bridge precedent — `scheme_zmq.py` (THE template)

`scheme_zmq` already solves "external async/blocking I/O source → synchronous pipeline":

```
create_sources:  bind zmq.PULL; RCVTIMEO=1000ms;
                 self.queue = queue.Queue(); self.terminate = False
                 Thread(target=self._run, daemon=True).start()
                 self.pipeline_element.create_frames(stream, frame_generator)
_run:            while not self.terminate:
                     try: self.queue.put(self.zmq_socket.recv())   # blocking, 1s timeout
                     except zmq.Again: continue
frame_generator: drain up to data_batch_size from self.queue;
                 records → OKAY; empty → NO_FRAME
destroy_sources: self.terminate = True                 # thread wakes via RCVTIMEO, exits
create_targets:  connect zmq.PUSH; stash socket in stream.variables
destroy_targets: close socket + term context
```

**This is exactly the shape `livekit://` needs**, substituting an asyncio LiveKit `Room`
loop for the ZMQ socket. Note the teardown discipline: a bounded socket timeout so the
`terminate` flag is actually observed (no hang on close). A `livekit://` source must
mirror this — signal the event loop to stop and `await room.disconnect()` with a bounded
join, so a stream restart never leaks a loop/thread.

## 3. Sibling + trust-boundary precedent — `scheme_http.py` (nick's fork PR, task #30)

The closest existing scheme, and the **contribution-path precedent** (built in a fork,
cage-matched, offered upstream to Andy). Critically, it is a **hardened trust boundary**:
- SSRF defense: `_resolve_public_ip()` blocks private/link-local; **pin the socket to the
  validated IP** while preserving hostname for SNI/cert/Host (DNS-rebinding-proof); reject
  `https→http` downgrade; validate **every redirect hop before contacting it**; one total
  deadline across connect+redirects+body; `session.trust_env = False`.
- It survived a **2-round 5-family cage-match that found real SSRF holes each round**.
- It is **source-only** (`create_targets` returns ERROR "not implemented") — a precedent for
  shipping one direction first.

**Lesson for `livekit://`:** a media scheme that takes a URL is a trust boundary. The
cage-match WILL hunt the injection/exfil analog. For `livekit://` the analogs are the **SFU
URL** (rogue-SFU token exfil), the **join token** (bearer-credential blast radius), and the
**room** (authorization/namespacing).

## 4. livekit-rtc API — PROVEN this session (`e2e_media.py`, both live SFUs)

- Connect: `room = rtc.Room(); await room.connect(url, token, options=rtc.RoomOptions(auto_subscribe=...))`. Needs a running asyncio loop.
- **Publish (DataTarget):** `src = rtc.VideoSource(w,h)`; `track = rtc.LocalVideoTrack.create_video_track("name", src)`; `await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=SOURCE_CAMERA))`; then per frame `src.capture_frame(rtc.VideoFrame(w, h, RGB24, bytes))`. **`capture_frame` is SYNChronous** (verified — called bare, no await, in the e2e test) → callable directly from a sync `process_frame`, provided the `Room`/source were created on the bridge loop.
- **Subscribe (DataSource):** `@room.on("track_subscribed")` → `rtc.VideoStream(track)` is an **async iterator** of frame events; `frame.convert(RGB24).data` → `np.frombuffer(...).reshape(h, w, 3)`. Requires the async loop running in the bridge thread.
- SDK has `VideoStream`, `VideoFrame` (`.convert`, `.data`), `VideoSource`, `VideoBufferType.RGB24` **and** `.RGBA` — confirmed via introspection.

## 5. aiko frame representation (`elements/media/video_io.py`)

`process_frame(self, stream, images)` — `images` is a **list of numpy RGB `ndarray`**
(explicit `isinstance(image, np.ndarray)`; assert "Image media_type must be a numpy array";
`image = np.array(image) # RGB`). So:
- **Publish:** `rtc.VideoFrame(w, h, RGB24, images[0].tobytes())` — **zero conversion** (RGB24
  is 3-byte packed RGB, matching a contiguous HxWx3 uint8 array).
- **Subscribe:** `np.frombuffer(frame.convert(RGB24).data, np.uint8).reshape(h, w, 3)`.

## 6. Token/identity — the island endpoint vs direct mint (deployed reality)

The island's `POST /v1/channels/{channel_id}/video-token` (deployed **v0.6.0** both islands,
media-proven this morning) returns `{token, url, room, can_publish}` and enforces: auth
(`CurrentUser`), existence-hiding ACL, **DM-only + exactly-2-party**, **block gate**, publish
gated on posting-membership, and **gateway_id-namespaced** room+identity. Alternatively the
bridge could mint directly with `LIVEKIT_API_KEY/SECRET` via `livekit_tokens.mint_room_token`.

| | Island endpoint | Direct `LIVEKIT_*` mint |
|---|---|---|
| Creds on bridge host | a **user session token** | **SFU master key/secret** |
| Blast radius if bridge host popped | one user's DM rooms | **forge ANY room token on the SFU** (mesh-wide) |
| Authorization enforced | island (DM/block/namespace) | none — raw mint |
| Requires | robot to hold a user identity/session | just the SFU creds |

The tension the design must resolve: **least-privilege wants the endpoint; operational
simplicity wants direct mint.** A robot is a "trusted server-side identity" — but "trusted"
is not "should hold the SFU master secret."

## 7. Prior art (wider world)

- LiveKit **Agents** framework already runs server-side participants that publish/subscribe
  tracks in Python — the exact "headless media participant" shape; confirms the pattern is
  first-class, not a hack. A `livekit://` DataScheme is essentially a minimal Agent wrapped
  in aiko's DataScheme interface.
- GStreamer `appsrc`/`appsink` is how aiko's own `elements/gstreamer/*` bridge frames in/out
  of native pipelines — same "adapter element" philosophy, different transport.
