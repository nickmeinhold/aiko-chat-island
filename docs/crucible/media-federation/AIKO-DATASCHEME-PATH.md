# Does the aiko crossover change the federation picture? (#3196)

*Read from source 2026-08-17: `aiko_services@ad95886` (`../aiko_services`), plus the
`livekit://` DataScheme design on `feat/crucible-livekit-datascheme`. Companion to
[`FORK-PRICING.md`](FORK-PRICING.md). Nick's question: "recognising that we will eventually
cross over to using it and have a DataScheme that speaks LiveKit, does that change anything?"*

**Yes. It changes the answer more than the estimate did — and the sharpest consequence is that
it makes the livekit-server fork strategically wrong, not just expensive.**

---

## 1. What aiko actually provides

### The DataScheme contract is tiny

`src/aiko_services/main/scheme.py` is ~70 lines. A `DataScheme` is registered against a URL
scheme in a `LOOKUP` table and implements four methods:

```python
create_sources(stream, data_sources, frame_generator=None, use_create_frame=True)
create_targets(stream, data_targets)
destroy_sources(stream)
destroy_targets(stream)
```

That is the entire extension surface. Existing schemes: `file`, `http`, `tty`, `zmq`, `rtsp`,
`colab`. Adding `livekit://` is a normal, expected extension — not a fork of anything.

### aiko pipelines ALREADY distribute across hosts

This is the part that matters and it is easy to miss. `src/aiko_services/main/pipeline.py`:

- `DEPLOY_TYPE_REMOTE = "remote"` — a `PipelineElement` can be deployed on **another host**
- `self.remote_pipelines = {}  # Service name --> PipelineRemote instance`
- remote elements are located by **`service_filter`** — i.e. the registrar, aiko's discovery
- the transport is **MQTT** ("low-level use of MQTT messages as remote function calls")

**aiko already has the distributed coordination layer that LiveKit Cloud built a custom
FlatBuffers pub-sub bus for.** Discovery, addressing, remote invocation, and a message bus
across independently-running hosts: present, working, and already the substrate this project's
gateway sits on.

## 2. The load-bearing fact: a DataScheme carries FRAMES, not RTP

Verified twice from source, because everything downstream depends on it.

**`scheme_rtsp.py`** builds this gstreamer chain:

```
rtspsrc ! rtph264depay ! h264parse ! <h264 decoder> ! videoconvert ! videorate ! appsink
```

That is a **full decode to raw frames**. And the `livekit://` design speaks the same language —
`VideoSource`, `capture_frame`, deep-copying frames before queueing, non-contiguous/wrong-dtype
publishes treated as errors. Raw buffers throughout.

**So an aiko media bridge is a TRANSCODING bridge, not a forwarding relay.** Decode in,
re-encode out, every hop. This is the single most important difference from an SFU, and it is
physics, not an implementation detail.

## 3. What this changes

### It deletes the fork

A bridge built as an aiko pipeline element is a **client of LiveKit's public SDK**. Every number
in `FORK-PRICING.md` that made the fork frightening simply stops applying:

| Fork path | aiko path |
|---|---|
| 87,188 LOC patched surface | none — public SDK only |
| 56% of upstream commits land in it | 0% |
| +14,242/−3,495 lines/6mo rebase tax | none |
| re-derive LiveKit Cloud's private `Session` split | not needed |
| implement 136-method `LocalParticipant` or do surgery on a concrete `Room` | not needed |

The federation topology becomes an aiko pipeline: **source = a room on island A, target = a room
on island B**, with the two ends discoverable and remotely deployable through machinery that
already exists.

### And it is the same shape as the thing that was already refused

`livekit/livekit#3484` ("Bridge Rooms") was closed as not planned, and the author's attempt was
*"join as a participant via the Go SDK and re-publish"* — which is exactly this. He hit
simulcast/SVC limits. **We would hit the same wall.** The difference is that we would hit it as a
supported client of a public API rather than as a fork, and — see below — we may not care.

## 4. What it costs, honestly

| Cost | Severity |
|---|---|
| **Transcode per hop** — decode + re-encode every stream | **the dominating cost.** An SFU forwards without touching the codec, near-free. A transcoding bridge is roughly a core per video stream. |
| **Latency** — a full decode/encode cycle added per hop | real, bounded, measurable |
| **Generation loss** — re-encoding degrades quality | real |
| **Simulcast dies** — one encoded output means no per-subscriber layer selection | *note: Octo has this same limitation after years in production, per webrtcHacks — it is not unique to this path* |

**Grounding on the actual boxes** (checked, not assumed): both islands are **4 cores / 23 GB**,
currently idle (load 0.20 and 0.00). That is enough for a **small-gathering** transcoding bridge —
a handful of streams. It is emphatically not enough for a large room. Which is fine, because
"gather people for a thing" at aiko's current scale means a handful of people. **This constraint
must be measured before it is designed against, not guessed.**

## 5. The reframe that makes the cost stop being a cost

For a pure relay, transcoding is dead loss — you paid a core to move bytes you could have
forwarded.

**But aiko's entire thesis is doing things to media streams.** ML pipelines, the robot loop,
transcription, moderation-in-the-media-path. An SFU forwards opaque encoded bytes and can *never*
do any of it. Frames are not an unfortunate side effect of the aiko path — frames are the point.

So the question inverts: **if media is going to pass through aiko anyway to get the features that
justify aiko, the decode is already paid — and federation falls out of the pipeline as a side
effect rather than being built as its own subsystem.**

You do not build a federation relay. You build a media pipeline, and it federates because aiko
already does.

## 6. What a bridge inherits from the existing (un-struck) design

A federation bridge is **precisely the duplex case** the `livekit://` Temper already flagged, so
two of its findings are directly load-bearing here and are already written down:

- **The duplex invariant.** *"one Room per (process, channel_id, island identity), shared by
  source+target — OR two distinct island identities."* A bridge by definition connects to two
  rooms at once; LiveKit is last-session-wins per identity. Get this wrong and one room silently
  dies and looks like a network fault.
- **A dedicated MACHINE principal**, not a human's session. A bridge is a machine participant and
  needs its own identity, with a real refresh/revocation lifecycle.
- Plus the `island_url` SSRF boundary and capability-scoped tokens, both already folded in.

That design is **UN-STRUCK** (a substantial post-Temper recast that has not been re-struck) and
is parked as #2828/#15 pending a round-2 strike or a handoff to Andy. **The federation question
promotes it from a robot-loop side quest to the critical path.**

## 7. The strategic consequence — this is the real finding

> **A livekit-server fork is a bet on a substrate we are planning to leave.**

The fork's rebase tax (~26 commits/month against our patch) runs *until the aiko crossover* — and
then the fork is dead weight, because the aiko path does not need it. We would be paying a
permanent maintenance cost, in Go, in someone else's fast-moving media core, to build a
capability that the architecture we are migrating toward provides differently.

That is a stronger argument against the fork than the cost was. Cost is a number you can decide
to pay; this is paying it for something you intend to throw away.

## 8. Revised recommendation

Three paths, and the ordering changed:

- **A — Host election (now).** A gathering elects one island's SFU; remote participants get
  tokens verified against their home island's signing key (#1816, `/v1/keys`). Weeks, no fork, no
  transcode, delivers the product property. **Do this first.**
- **C — aiko pipeline bridge (the destination).** Federation as a `livekit://` DataScheme plus
  aiko's existing remote-pipeline deployment. Arrives with the aiko crossover; the transcode cost
  is already paid by the features that motivate the crossover. **Aim here.**
- **B — Fork livekit-server. No.** Months, permanent rebase tax, the only genuinely unsolved
  piece (cross-operator trust) invented from scratch, and obsoleted by the crossover.

**Nothing is wasted.** A's cross-island authorization is the same trust layer C needs to let one
island's pipeline pull from another's room. Build the auth once; host-election ships on it now,
the aiko bridge inherits it later.

### Next concrete step
Round-2 strike on the `livekit://` DataScheme re-cast (#2828), now that it sits on the critical
path rather than beside it — or hand it to Andy with this federation context attached, since it is
his repo and the framing has changed.
