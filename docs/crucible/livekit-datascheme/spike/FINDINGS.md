# SPIKE FINDINGS — `webrtc://` DataScheme, measured

Status: **spike complete, design NOT yet re-struck.** These are measurements, not a
verdict on the design. Run 2026-08-20 against a real `livekit-server v1.13.5` (local,
`--dev`), a real SFU hop, and real VP8 — not a mock.

Why this exists: Temper round 1 overturned the design's mechanism on a claim nobody had
run. Three model families agreed on it, which is corroboration between reasoners, not
evidence about the system. `feedback_verify_prod_before_multi_round_review` — a probe
finds contract mismatches that a review panel sharing the author's premise cannot.

## What was built

```
publisher.py --VP8--> SFU --VP8--> sidecar.py --zmq--> pipeline side
```

`sidecar.py` is the out-of-process bridge the re-cast chose. It PUSHes to a PULL socket
the pipeline side binds — the same socket direction as `scheme_zmq`, which is what makes
it a drop-in `zmq://` producer rather than a bespoke protocol.

## F1 — the PRIMARY falsifier is HALF TRUE, and the half that is false is the half that drove the re-cast

The claim: *"a hung `disconnect()` orphans the thread/loop/peer-connections and restart
storms leak."* Two claims in one sentence. They do not both hold.

| | clean teardown, 16 cycles | hostile teardown, 8 cycles |
|---|---|---|
| in-process, threads/cycle | +0.07 | **+0.00** |
| in-process, fds/cycle | +0.00 | **+0.00** |
| out-of-process, threads/cycle | −0.07 | +0.00 |
| out-of-process, fds/cycle | +0.07 | +0.00 |

**The hang is REAL and total.** Black-hole the SFU mid-session (`docker pause` — packets
vanish; deliberately not `docker stop`, whose TCP RST completes the close promptly and is
the easy case) and `await room.disconnect()` **never returns: 8/8 cycles at a 10s timeout,
and still hung at 90s.** No internal timeout rescues it. The Temper was right about this.

**The leak is NOT real *for this teardown shape*.** Under those same hung teardowns, thread
and fd growth is **exactly zero per cycle** in both arms.

> **SCOPE CORRECTION (F10, later the same day).** That zero is real but narrower than it
> reads. `run_teardown.py` black-holes the SFU and then HEALS it, which flushes the buffered
> leave — so the server sees a clean departure and the client's session is genuinely torn
> down. Under a *true* orphan (drop the reference, never disconnect, process stays alive)
> the same process leaks **43 fds per cycle**. The original "0.00 fds" was measured in a
> condition that does not produce the leak. See F10.

So the in-process shape's actual defect is a **liveness** defect, not a resource one. That
matters because the two have different fixes: a leak needs process isolation, a hang needs
a bounded wait. The re-cast bought process isolation to solve a problem that turns out to
be a hang.

## F2 — the out-of-process shape does NOT inherit an escape from the hang (design hole)

The re-cast's reasoning was "the OS reaps what Python cannot." Measured, that is true only
under a condition the design never states.

**The sidecar hung on SIGTERM in 8/8 hostile cycles, and was still hung at 90s.** Every
child exited `rc=-9` — SIGKILL. It survived SIGTERM because its handler sets a stop flag
and then awaits the same `room.disconnect()` that hangs in-process. The hang did not go
away; it moved into a child.

The out-of-process benefit is therefore real but far narrower than stated:

> You can `SIGKILL` a process. You cannot `SIGKILL` a coroutine.

That is the whole of it — and it is **conditional on the supervisor escalating**. A
supervisor that sends SIGTERM and waits inherits the identical unbounded hang. DESIGN.md
says "supervised out-of-process helper" and never specifies kill escalation. **That is a
hole this measurement found and the Temper did not:** the escalation is not a hardening
detail, it is the entire mechanism by which the chosen shape works at all.

## F3 — the frame path is correct, proven against a known answer

`PIXEL_OK` on 6/6 runs, both wire formats. Max per-quadrant error **1.0–2.0 / 255** — VP8
noise. The embedded sequence marker is recovered exactly through the lossy codec, so
frames are individually identifiable end to end.

The comparator was positive-controlled before its green was trusted (an instrument that
cannot go red proves nothing):

| injected fault | max quadrant error | verdict at threshold 25 |
|---|---|---|
| codec-like noise | 1.7 | passes (correct) |
| R/B channel swap | 126.7 | **caught** |
| vertical flip | 130.0 | **caught** |
| horizontal flip | 166.7 | **caught** |
| stride padding 1/4/16px | 135.5 / 89.6 / 82.9 | **caught** |

Known blind spot, stated rather than discovered later: a *uniform* sub-block translation
(roll by 7px) scores 3.65 and would pass. No adapter bug produces a pure uniform
translation — a real stride bug shears progressively, which is the row above — but the
instrument is genuinely insensitive to that transform.

**Correction to the design.** Fold claim E specified "assert pixels match". That assertion
cannot pass: WebRTC video is lossy by construction, so byte-equality fails no matter how
correct the adapter is. The test has to be a similarity budget with a positive control,
which is what was built. The discipline behind claim E was right; its acceptance criterion
was not achievable.

## F4 — decode-to-frames is CHEAP, and our question to Andy was built on a wrong estimate

Task #3247 planned to ask Andy whether decode-to-frames is deliberate, on the premise that
"a two-island media bridge transcodes at roughly a core per stream." **Measured, that
premise is wrong at 640×480:**

| step | cost/frame | note |
|---|---|---|
| `frame.convert(RGB24)` → numpy | **0.27–0.33 ms** | I420→RGB24 + copy |
| JPEG encode (aiko's zmq wire) | **1.10–1.24 ms** | vs 0.95 ms same-process control |
| raw RGB (no re-encode) | 0.04 ms | 900 KiB/frame on the wire |

At 30fps that is ~1% of a core for the conversion and ~4% including aiko's JPEG hop —
not "a core per stream". **The question to Andy should be re-derived or dropped**; asking
it as framed would be asking him to solve a cost that was mis-estimated by ~25×.

**Scope limit, and it is important:** `frame.convert()` measures the colour conversion
only. The **VP8 decode already happened on a native SDK thread** before the frame reached
the async iterator, so no in-loop timer can see it. Whole-process CPU accounting was added
for this reason (`cores_used` in `SIDECAR_STATS`) and is the only number that counts the
native threads. The per-step timings above are honest about their layer; do not quote them
as "the decode cost".

## F5 — aiko's zmq image convention puts a SECOND lossy codec in series with VP8

`image_io.ImageWriteZMQ` sends `image_to_bytes(image)` = `PIL.save(format="JPEG")`, and
`ImageReadZMQ` decodes it. So an out-of-process bridge that is wire-compatible with aiko's
existing pipeline elements re-encodes every frame to JPEG after VP8 already decoded it.

Not fatal — 1.2ms/frame, and the pixel test still passes at 1.0 MAE — but it is a real
architectural consequence the design never priced, and it forces a fork:

- **`--wire jpeg`** — works with `ImageReadZMQ` today, zero new upstream code, two lossy
  codecs in series, ~6 KiB/frame.
- **`--wire raw`** — one codec, 0.04ms, but 900 KiB/frame and needs a new media type
  upstream, i.e. it is a change to Andy's repo, not just ours.

This fork is a genuinely good thing to put to Andy, and unlike F4's question it rests on a
measurement rather than an estimate.

## F6 — `webrtc://` works as a real DataScheme, and aiko's OWN element consumes it

`scheme_webrtc.py` registers `webrtc` in `aiko.DataScheme.LOOKUP`. `pipeline_webrtc.json`
then runs under a real broker and a real `aiko_pipeline create -s 1`, and its source
element is **aiko's own unmodified `image_io.ImageReadZMQ`** — re-exported, not subclassed,
not wrapped. The only change from the stock zmq pipeline is one string:

```
"data_sources": "(zmq://localhost:6502)"   ->   "(webrtc://<room>)"
```

Result: `PIPELINE_VERDICT=PIPELINE_PIXEL_OK`, 5/5 frames delivered by the pipeline,
max quadrant error 1.3–5.1, `orphaned_sidecars_after_run: 0`. The design's central claim —
*first-class-ness lives at the URL + registration layer* — **holds**: an element that has
never heard of WebRTC read video out of a LiveKit room.

Controls, because a green from an instrument I wrote is not evidence:

- **Negative control** (`--no-publisher`): fails closed with
  `ERROR ... webrtc:// sidecar not ready within 30.0s`. Never reaches PIXEL_OK.
- **Orphan check** runs only after the pipeline is reaped (checking earlier would report a
  false positive every run, since a live sidecar under a live pipeline is correct).

**Worth putting to Andy:** the element that works here is called `ImageReadZMQ`. It is
already transport-agnostic — `process_frame` only calls `bytes_to_image(record)` and never
touches zmq, exactly matching his "the DataScheme references the transport" model. But the
NAME still leaks a transport it does not depend on, so reading from `webrtc://` currently
requires an element called `...ZMQ`. The generic element wants a transport-neutral name.

**Also found:** aiko's `get_network_port_free` calls `psutil.net_connections`, which
raises `AccessDenied` for an unprivileged process on macOS — so any scheme using it cannot
start a stream on a dev box. Binding `tcp://127.0.0.1:*` and reading `LAST_ENDPOINT` back
from zmq avoids the privileged call and removes the helper's check-then-bind race.

## F7 — the out-of-process shape LEAKS on the hard-kill path (the inverse of F1)

This is the sharpest finding of the spike and it cuts against the re-cast.

`destroy_sources` → the supervisor → the SIGKILL escalation all run on aiko's graceful
path: a stream `STOP` schedules `destroy_stream` with `delay=3.0`, which calls
`stop_stream` → `destroy_sources`. Verified — it is called, and it reaps the child.

But **aiko's Pipeline installs no SIGINT/SIGTERM handler** (none in `pipeline.py` or
`process.py`). So on any hard kill of the pipeline process, `destroy_sources` never runs.
Measured: SIGKILL the pipeline and **the sidecar survives, indefinitely** — still joined to
the room, still decoding, still holding a live SFU participant seat.

That is an inversion of the whole re-cast argument:

| | in-process Room | out-of-process sidecar |
|---|---|---|
| hung `disconnect()` | unbounded hang, no leak | **better** — SIGKILL always wins |
| pipeline hard-killed | Room dies with the process | **worse** — live SFU seat orphaned |

The out-of-process shape did not remove the leak the Temper feared; it **moved** it to a
different trigger, and put it somewhere the pipeline can no longer clean up. In-process, a
killed pipeline takes its Room with it for free.

**Fixed, with a red-then-green proof.** The sidecar now runs a parent-death watchdog
(`getppid()` change ⇒ reparented ⇒ stop) and bounds its own `disconnect()` at 5s.
Before: `ORPHANED_BY_SIGKILL=True`, still orphaned at 15s. After: `False`, reaped in
**1.1s**. The guard was proven to matter by observing it genuinely fail first.

Note the first version of that watchdog **did not work**, for an instructive reason: it
`print()`ed before setting the stop flag, and stdout is a pipe held by the parent that just
died — so `BrokenPipeError` killed the watchdog task before it could stop anything, leaving
exactly the orphan it existed to prevent. State first, then log, and logging must never be
able to prevent exiting.

## F8 — the abandoned Room's seat is IMMORTAL, and that makes the reaper mandatory

Round 3 struck D1 4/4 on the claim that "declared dead" is a local statement the SFU never
hears. Measured (`measure_seat.py`), against the server's own roster via `ListParticipants`:

| what happened to the client | seat freed after |
|---|---|
| clean `disconnect()` — **positive control** | **0.0 s** |
| process SIGKILLed (OS closes the socket) | **20.1 s** |
| process SIGSTOPped (socket open, no answers) | **22.1 s** |
| **Room abandoned, process still alive — D1's case** | **no departure observed in 240 s** |

The control matters: it proves the poller can *see* a departure, so "still seated" is a
finding rather than a blind instrument.

**LiveKit reaps a silent peer in ~20 s. But an abandoned Room in a living process is not
silent** — its native WebRTC threads keep answering keepalives, so the SFU sees a perfectly
healthy participant and never reaps it. D1's ghost is therefore **not a bounded 20-second
nuisance; it is unbounded**, and it is *worse* than the sidecar orphan the design rejected:
a sidecar orphan dies when something kills it, while this one is actively kept alive by a
process that is working correctly.

Claim scope: "no departure observed in 240 s" — more than 10× the silent-peer reap, with the
mechanism (answered keepalives) understood. That is enough to establish a different regime; it
is not a proof of "forever", and it is not claimed as one.

**Consequence for the design:** the seat reaper stops being a good idea the panel suggested and
becomes the only thing that makes D1 viable. Without it, D1's fail-closed restart policy denies
the capability indefinitely — the self-DoS all four families predicted.

## F9 — the reaper works, and cannot overreach

`reaper.py` + `test_reaper.py`. Built a real immortal ghost, then reclaimed it. Red-then-green
with the over-reach controls, because the reaper authenticates with **API master credentials**
and an over-broad one would be a room-wide kick primitive on every host running a pipeline:

| check | result |
|---|---|
| ghost genuinely seated before the reap (RED control) | `robot-7#1` present |
| ghost evicted | yes, in **0.01 s** |
| bystander `someone-else` untouched | survived |
| `keep` identity `robot-7#2` untouched | survived |
| reap against a nonexistent room | success, not error |

**Two design properties, deliberately:**

- **Epoch before reap.** The scheme mints `<base>#<epoch>` and connects as a NEW generation, so
  epoch N+1 can never collide with epoch N's corpse under last-session-wins. The reap is then
  *cleanup*, not a precondition — correctness never depends on it succeeding, so a failed reap
  costs a lingering ghost rather than a broken stream.
- **The reaper can only reclaim its own base identity.** Not a moderation tool. Anything
  broader puts room-wide eviction on a robot host.

**Named, not assumed:** the island has never called the LiveKit server API, and
`mint_room_token` deliberately withholds `roomAdmin`/`roomList`. `RemoveParticipant` needs the
API key/secret — master credentials for every room on that SFU — which is a real argument that
the **island** should own the reaper (it already holds those creds and is the token authority
under D3) rather than every pipeline host holding them.

Integrated into `scheme_webrtc.create_sources` and re-verified end to end: `PIPELINE_PIXEL_OK`,
5/5 frames, 0 orphaned sidecars, and the negative control still fails closed.

## F10 — G1: the reaper closes a 43-fd/cycle leak and 70% of the RSS drift, but G1 stays OPEN

F8 gave G1 its first hypothesis: *the ~0.4 MB/cycle RSS drift is the immortal Rooms, and the
reaper closes it.* Tested with two arms, 20 cycles each, true orphan abandonment (drop the
reference, no disconnect, process alive — the only shape F8 proves strands the seat), **each
arm in its own process** and run in both orders, because a shared process lets the second arm
inherit whatever the first leaked.

| | NO_REAP | REAP |
|---|---|---|
| ghost seats after 20 cycles | **20** — one per cycle | **0** |
| file descriptors per cycle | **+43.1** | **0.0** |
| RSS per cycle | **1.428 MB** | **0.428 MB** |

**Three results, and only one of them is the one I went looking for.**

**1. Epoch alone converts a collision into a leak — confirmed, and it is the strongest argument
for the reaper.** Epoch identities were introduced so a new bridge cannot collide with the
corpse under last-session-wins. But that means the corpse is no longer *overwritten*: without a
reaper the ghosts **accumulate exactly one per restart**, 20 after 20 cycles, each holding a
seat forever. The failure mode is not "memory grows" — it is **the room fills up with dead
robots**, and a room at max-participants is a room nobody living can join. The reaper takes
that to zero.

**2. A 43-fd-per-cycle leak nobody had seen, and it is an operational wall, not a drift.** At
43 fds/cycle a default 1024 limit is exhausted in ~24 stream restarts and macOS's 256 in ~6.
The reaper closes it **completely** (0.0/cycle). This one is a correction to my own earlier
claim: F1 reported "0.00 fds/cycle" and that number was measured under the *weak* abandonment
where the leave flushes on heal, so the condition that produces the leak never existed in that
test. A green from a test that cannot create the failure is not evidence of its absence.

**3. G1 is NOT closed — and the hypothesis is only 70% right.** The reaper removes exactly
1.0 MB/cycle of the 1.428, so most of the drift in the orphan condition really was the immortal
Rooms. But a **residual 0.428 MB/cycle survives the reaper** — and that is, to three decimal
places, the *original* G1 number (0.389 / 0.400 / 0.422 / 0.428 across four independent runs).
So the drift G1 was opened for is a **separate, still-unexplained leak** that has nothing to do
with the ghosts. It is remarkably stable across conditions, which is itself a clue: something
retains ~0.4 MB per connect/abandon cycle regardless of whether the session was cleanly closed,
stranded, or reaped.

**Disposition:** the reaper is now load-bearing for two independent reasons (seat exhaustion,
fd exhaustion) rather than one. G1 remains an open merge blocker on the in-process default,
with its scope narrowed from "an unexplained 0.4 MB/cycle" to "an unexplained 0.4 MB/cycle
that is NOT the SFU session, because it survives eviction."

## F11 — G1 RESOLVED: the residual is an upstream `livekit-rtc` leak, and it hands D6 its real justification

The clue was the stability: 0.389 / 0.400 / 0.422 / 0.428 MB per cycle across four runs whose
teardowns had nothing in common. A leak indifferent to *how a session ends* is probably not in
the session. So: bisect the cycle instead of soaking it, each arm doing strictly less than the
one above, each in its own process.

| arm | RSS/cycle |
|---|---|
| `NOTHING` — empty loop, **the null control** | **0.0** |
| `MINT_ONLY` — build a token, no Room | 0.0 |
| `ROOM_ONLY` — `rtc.Room()` constructed, never connected | 0.0 |
| **`CONNECT_ONLY` — connect + immediate clean disconnect** | **0.400** |
| `FULL` — connect + hold + media + disconnect | 0.402 |

**The whole leak is `connect()`→`disconnect()`. Media and hold contribute nothing (0.402 vs
0.400), and it is present under a perfectly CLEAN disconnect** — which is exactly why it was
identical under stranding and eviction. It was never about abandonment.

**And it is linear, not a warm-up.** 100 cycles: 91.45 → 130.19 MB, with per-10-cycle growth of
4.66 / 3.28 / 3.81 / 3.96 / 3.62 / 3.91 / 4.06 / 3.81 / **4.13** MB. The last decade grows as
fast as the first. No plateau, no asymptote — a genuine leak.

**The null control is what makes this trustworthy.** `NOTHING` reads exactly 0.0, so RSS *is* a
usable instrument at this resolution here. Every previous G1 measurement lacked that control
and therefore could not distinguish "a real leak" from "RSS goes up when a process does work".

**Scope:** `livekit-rtc` 1.1.14, CPython 3.13, macOS, against `livekit-server` v1.13.5. Not
verified on Linux, not bisected below the Python binding into the native SDK, and not checked
against upstream's issue tracker.

### What it means for the design

**G1 closes as "not ours".** The drift is an upstream defect, ~0.4 MB per **stream restart**
(not per second, not per frame). A pipeline that restarts a stream 10,000 times accumulates
~4 GB. That is a **process-recycle policy**, not something the DataScheme can fix.

**And here is the twist: this is the first measured argument FOR the sidecar.** The out-of-
process shape spawns a fresh process per stream, so the leak is reclaimed by the OS at every
teardown — **D6 is immune to it by construction**, while the in-process spine accumulates it
forever.

Round 1 chose the sidecar because "in-process leaks". That was *false as argued* (threads and
fds are flat) and the claim was correctly reversed in round 2. But there **is** a leak, in a
place nobody looked, and process recycling **does** fix it. Round 1 was right for the wrong
reason, and it took three temper rounds plus a day of measurement to find the right one.

This does **not** flip the default back — the in-process spine can adopt an explicit recycle
policy, and 0.4 MB per restart is affordable for most deployments. But it means:

- **D1 owes a stated recycle policy** ("after N stream restarts, recycle the process"), with N
  derived from this rate and the deployment's memory budget.
- **D6 gains a legitimate, measured justification** to replace the false one it was born with:
  *process recycling reclaims an upstream leak the in-process shape cannot*.

## Instrument failures caught (recorded because they were nearly reported as results)

1. **`encode_ms` = 64 ms/frame** in the first JPEG run — a 50× overstatement. PIL encodes
   this frame in 1.1ms standalone. Caught by benchmarking the codec in isolation and then
   adding a same-process calibration encode before any media thread exists. Without that
   control an in-loop timing cannot be attributed to the codec versus contention.
2. **"Out-of-process LEAKS, in-process is FLAT"** — a spectacular reversal of the Temper,
   and entirely my harness: `subprocess.Popen(stdout=PIPE)` was never closed (one pipe fd
   per cycle in the parent) and the PULL socket was never drained (900 KiB frames
   accumulating as parent RSS). The harness was leaking what it was measuring. Fixed, and
   both arms then read FLAT.

3. **"`destroy_sources` is never called"** — asserted from a grep that returned 0, twice.
   Both were capture artifacts: first the harness terminated the pipeline before aiko's
   `delay=3.0` `destroy_stream` could fire, then it stopped reading the pipeline's stdout
   after the verdict, so a later print was simply never recorded. The real answer is that
   it IS called on the graceful path and is NOT called on hard kill (F7) — a distinction
   both bad greps flattened into a single wrong "never".
4. **An orphan probe that measured ZOMBIES as alive** — it used `kill -0`, which succeeds
   for an unreaped dead child, and did not pass `LK_*` through, so the sidecar died at
   startup and was scored "alive, then killed by parent death". Two independent defects
   producing one plausible answer. Fixed by reading `ps -o state=` and inheriting the env,
   which reversed the conclusion.

5. **A G1 soak whose arms never built the thing under test.** The first run used the
   pause/abandon/unpause teardown and reported `seats=0` on every cycle in BOTH arms — the
   leave flushes when the network heals, so there were no ghosts to reap and the reaper
   "made no difference". The arms were not what they claimed to be. Re-run with true orphan
   abandonment, the same comparison shows 20 ghosts vs 0 and a 43-fd/cycle leak vs none.

All five shared a shape: the instrument's own defect was indistinguishable from the result
it was built to find. And three of them were *the same defect twice removed* — a teardown
that heals the network is not a teardown that strands a session, and I built that mistake
into three separate harnesses before naming it. Three of them produced a CONFIDENT, PLAUSIBLE, WRONG answer rather
than an obvious failure — which is why each needed a control rather than a re-read.

## What is NOT proven

- **Longevity.** In-process shows a small steady RSS growth (~0.37–0.47 MB/cycle) that
  out-of-process does not (~0.03). Over 16 cycles that is noise; over 10k stream restarts
  it is not. Threads/fds are flat, so this is not the claimed leak — but it is unexplained
  and needs a long soak before anyone calls in-process safe.
- **Publish direction.** Only subscribe (LiveKit → aiko) was measured. `capture_frame` is
  the easy half but it is unmeasured here.
- **The duplex collision.** Untested. Two `Room.connect()` on one identity is still an
  open Tesla finding.
- **Publish direction as a DataTarget.** `webrtc://` is a DataSource only here; the
  aiko -> LiveKit direction is unimplemented.
- **Any island integration.** Tokens are minted directly from the SFU's dev key. The
  island-endpoint capability-token model, the allowlisted origin, and the dedicated
  machine principal are all untouched — this spike deliberately isolates the transport.
- **Scale.** 640×480, one track, one subscriber, local SFU, macOS. No claim about 1080p,
  multi-track, or a real network.

## Reproducing

```bash
docker run -d --name lk-spike -p 7880:7880 -p 7881:7881 -p 7882:7882/udp \
  livekit/livekit-server:v1.13.5 --dev --bind 0.0.0.0 --node-ip 127.0.0.1
python -m venv venv && venv/bin/pip install livekit livekit-api numpy pyzmq psutil pillow
export LK_URL=ws://127.0.0.1:7880 LK_API_KEY=devkey LK_API_SECRET=secret

venv/bin/python run_pixel.py --wire jpeg      # F3, F4, F5
venv/bin/python run_teardown.py --cycles 16   # F1 clean path
venv/bin/python run_hostile.py --cycles 8     # F1, F2 — the claim that mattered

# F6/F7 — the real aiko Pipeline. Needs a broker too:
/opt/homebrew/sbin/mosquitto -c ../../../../spike/mosquitto.conf -d   # :1884
export AIKO_MQTT_HOST=localhost AIKO_MQTT_PORT=1884
venv/bin/pip install -e /path/to/aiko_services opencv-python-headless
venv/bin/python run_pipeline.py --frames 5                 # must be PIPELINE_PIXEL_OK
venv/bin/python run_pipeline.py --frames 5 --no-publisher  # must FAIL CLOSED
```

`--dev` uses the published placeholder keys `devkey`/`secret`. That is correct for a
throwaway local SFU and must never be pointed at an island.
