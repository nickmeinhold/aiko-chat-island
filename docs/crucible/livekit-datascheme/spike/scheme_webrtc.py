"""`webrtc://` — a real aiko DataScheme, registered in the real registry.

This is the integration half of the spike. `sidecar.py` proved the frame path and the
teardown behaviour against a real SFU; this makes `webrtc://` an actual entry in
`aiko.DataScheme.LOOKUP`, so a Pipeline dispatches to it by URL scheme exactly as it does
for `file://`, `zmq://`, `http://` and `rtsp://`.

The point being tested is the design's central claim:

    first-class-ness lives at the URL + registration layer, not at asyncio-in-pipeline

If that claim is true, an EXISTING, UNMODIFIED aiko element must be able to consume
`webrtc://` without knowing it exists. It can: this scheme emits the same record shape
`scheme_zmq` does (JPEG bytes per frame), so `image_io.ImageReadZMQ` — whose
`process_frame` just calls `bytes_to_image(record)` — decodes them unchanged. No fork of
aiko, no new element required for the source path.

Shape (Temper round-1 re-cast, decision 1): `create_sources` owns a SUPERVISED
OUT-OF-PROCESS helper and frames arrive over the already-solved local zmq path. The
pipeline process never imports livekit, never runs an asyncio Room, and never holds a
native WebRTC thread.

Three behaviours here are measurements from FINDINGS.md made structural, not defensive
extras:

  * **Kill escalation (F2).** The spike measured the sidecar HANGING on SIGTERM in 8/8
    hostile teardowns, still hung at 90s, every child ultimately reaped by SIGKILL. The
    out-of-process shape's entire advantage is "you can SIGKILL a process, you cannot
    SIGKILL a coroutine" — which is worth nothing unless someone actually escalates.
    `destroy_sources` escalates. A SIGTERM-and-wait supervisor would inherit the exact
    unbounded hang the re-cast was chosen to escape.
  * **Fail closed on a half-open source (Fold-B).** `create_sources` blocks on a bounded
    readiness signal from the child and returns ERROR on timeout. Never OKAY on a source
    that is not actually carrying frames.
  * **A dead link must not masquerade as idle (Fold-A).** If the child exits, the
    generator returns ERROR — not NO_FRAME forever, which is indistinguishable from
    "the peer has not published yet".
"""
from __future__ import annotations

import os
import queue
import signal
import struct
import subprocess
import sys
import threading

import aiko_services as aiko
import zmq


__all__ = ["DataSchemeWebRTC"]

_LOGGER = aiko.process.logger(__name__)

WIRE_HEADER = struct.Struct("!HHI")   # must match sidecar.py
SIDECAR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sidecar.py")

READY_TIMEOUT_S = 30.0      # bounded wait for the child to report subscribed
TERM_GRACE_S = 5.0          # how long SIGTERM gets before SIGKILL (F2)
KILL_GRACE_S = 5.0          # how long SIGKILL gets before we admit defeat loudly


class DataSchemeWebRTC(aiko.DataScheme):
    """`webrtc://<room>` — subscribe to a room's video track as aiko frames."""

    def create_sources(self, stream, data_sources,
                       frame_generator=None, use_create_frame=False):
        if not frame_generator:
            frame_generator = self.frame_generator

        room = aiko.DataScheme.parse_url_path(data_sources[0])
        if not room:
            return aiko.StreamEvent.ERROR, {
                "diagnostic": f'webrtc:// URL "{data_sources[0]}" must be "webrtc://<room>"'}

        # The pipeline side BINDS PULL and the child connects PUSH — the same direction
        # scheme_zmq uses, which is what lets an existing element consume these records.
        #
        # Port selection deliberately does NOT use aiko's `get_network_port_free`: it
        # calls `psutil.net_connections`, which raises AccessDenied for an unprivileged
        # process on macOS, so any scheme using it cannot start a stream on a dev box.
        # Binding `:*` lets zmq pick the port in the kernel and report it back, which also
        # removes the check-then-bind race the helper has by construction.
        self.zmq_context = zmq.Context()
        self.zmq_socket = self.zmq_context.socket(zmq.PULL)
        self.zmq_socket.setsockopt(zmq.RCVTIMEO, 1000)
        self.zmq_socket.setsockopt(zmq.RCVHWM, 8)
        self.zmq_socket.bind("tcp://127.0.0.1:*")
        self.zmq_url = self.zmq_socket.getsockopt(zmq.LAST_ENDPOINT).decode()
        self.share["webrtc_room"] = room
        self.share["webrtc_zmq_url"] = self.zmq_url

        wire, _ = self.pipeline_element.get_parameter("wire", "jpeg")
        self.queue = queue.Queue()
        self.terminate = False
        self.child_died = False

        self.child = subprocess.Popen(
            [sys.executable, SIDECAR, "--wire", str(wire), "--zmq", self.zmq_url,
             "--identity", f"webrtc-scheme-{os.getpid()}"],
            env=dict(os.environ, LK_ROOM=room),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        _LOGGER.debug(f"create_sources(): sidecar pid={self.child.pid} room={room} "
                      f"zmq={self.zmq_url} wire={wire}")

        # FAIL CLOSED on a half-open source: block, bounded, on the child's own readiness
        # signal. Returning OKAY before the child has subscribed would hand the pipeline a
        # source that silently produces nothing.
        ready = threading.Event()
        threading.Thread(target=self._watch_child, args=(ready,), daemon=True).start()
        if not ready.wait(READY_TIMEOUT_S):
            self._reap()
            return aiko.StreamEvent.ERROR, {
                "diagnostic": f"webrtc:// sidecar not ready within {READY_TIMEOUT_S}s"}
        if self.child_died:
            self._reap()
            return aiko.StreamEvent.ERROR, {
                "diagnostic": "webrtc:// sidecar exited before subscribing"}

        threading.Thread(target=self._run, daemon=True).start()
        self.pipeline_element.create_frames(stream, frame_generator)
        return aiko.StreamEvent.OKAY, {}

    def _watch_child(self, ready: threading.Event) -> None:
        """Relay the child's stdout, and set `ready` on its subscribe signal.

        Also the ONLY place that learns the child died — a silently dead child is the
        Fold-A failure (dead link reading as idle), so it is recorded, not swallowed.
        """
        for line in self.child.stdout:
            line = line.rstrip()
            if line:
                _LOGGER.debug(f"[sidecar] {line}")
            if "SIDECAR_SUBSCRIBED" in line:
                ready.set()
        self.child_died = True
        ready.set()      # unblock create_sources so it can fail rather than wait out the timeout

    def _run(self) -> None:
        while not self.terminate:
            try:
                payload = self.zmq_socket.recv()
            except zmq.Again:
                continue
            except zmq.ZMQError:
                break
            _w, _h, length = WIRE_HEADER.unpack_from(payload, 0)
            # Strip the transport header: what reaches the queue is exactly what
            # scheme_zmq would have queued, so downstream elements are unmodified.
            self.queue.put(payload[WIRE_HEADER.size:WIRE_HEADER.size + length])

    def frame_generator(self, stream, frame_id):
        data_batch_size, _ = self.pipeline_element.get_parameter("data_batch_size", default=1)
        records = []
        for _ in range(int(data_batch_size)):
            if not self.queue.qsize():
                break
            records.append(self.queue.get())

        if records:
            return aiko.StreamEvent.OKAY, {"records": records}
        # A DEAD LINK MUST NOT MASQUERADE AS IDLE (Fold-A): NO_FRAME here is
        # indistinguishable from "the peer has not published yet", so a dead child has to
        # be its own event or the stream hangs looking healthy forever.
        if self.child_died:
            return aiko.StreamEvent.ERROR, {"diagnostic": "webrtc:// sidecar died"}
        return aiko.StreamEvent.NO_FRAME, {}

    def destroy_sources(self, stream):
        # Whether aiko invokes this at all is the question the supervision depends on:
        # it IS called on the graceful destroy_stream path, and is NOT called on a hard
        # kill (no signal handler in aiko's Pipeline) — see FINDINGS.md F7, which is why
        # the sidecar also watches for its parent dying.
        _LOGGER.debug("destroy_sources(): reaping webrtc:// sidecar")
        self.terminate = True
        self._reap()
        if getattr(self, "zmq_socket", None):
            self.zmq_socket.close()
            self.zmq_socket = None
        if getattr(self, "zmq_context", None):
            self.zmq_context.term()
            self.zmq_context = None

    def _reap(self) -> None:
        """SIGTERM, then SIGKILL. The escalation IS the mechanism (F2).

        Measured: with the SFU black-holed, the sidecar ignores SIGTERM indefinitely
        (8/8 cycles, still hung at 90s) because its handler awaits the same
        `room.disconnect()` that hangs in-process. A supervisor that only sends SIGTERM
        and waits reproduces the unbounded hang this whole shape exists to avoid.
        """
        child = getattr(self, "child", None)
        if child is None or child.poll() is not None:
            return
        child.terminate()
        try:
            child.wait(timeout=TERM_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            _LOGGER.info(f"webrtc:// sidecar pid={child.pid} ignored SIGTERM after "
                         f"{TERM_GRACE_S}s — escalating to SIGKILL")
        child.kill()
        try:
            child.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            # Uninterruptible sleep is the only way past SIGKILL. Loud, never silent —
            # a leaked child across stream restarts is the failure mode under test.
            _LOGGER.error(f"webrtc:// sidecar pid={child.pid} SURVIVED SIGKILL")
        finally:
            try:
                child.stdout.close()   # else the parent leaks a pipe fd per stream
            except Exception:
                pass


aiko.DataScheme.add_data_scheme("webrtc", DataSchemeWebRTC)
