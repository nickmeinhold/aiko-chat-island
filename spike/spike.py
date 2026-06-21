#!/usr/bin/env python3
"""Phase 0 de-risking spike — resolve aiko echo semantics (the Phase 1 GATE).

This is the single empirical question the whole gateway ingest/dedupe design
hangs on (plan §A5, §"Riskiest unknowns" #1):

  When the gateway publishes a chat message (as some username) into the aiko
  bus, and is ALSO subscribed to that channel topic to receive other users'
  messages, does it receive *its own* message back, and is the ``username``
  field preserved byte-exact?

If yes  -> the gateway must dedupe its own echoes; the redis key
           ``(aiko_channel, aiko_username, body_hash)`` is viable ONLY if
           ``username`` survives byte-exact.
If no   -> echo suppression (Layer A) is unnecessary; simpler.

The client is lifted from aiko-chat-bridge/aiko_bridge/aiko_client.py (the
proven headless-client pattern: a BridgeChatActor on a daemon thread owning
aiko's blocking event loop; discovery via the Registrar; send via the
discovered ChatServer proxy).

Run via run_spike.sh, which stands up the supporting stack (mosquitto +
aiko_registrar + aiko_chat ChatServer) first. Requires AIKO_NAMESPACE=aiko
and AIKO_MQTT_HOST=localhost in the environment (set by the script).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

import aiko_services as aiko
from aiko_chat.chat import ChatServer, get_server_service_filter

CHANNEL = "general"
SEND_AS_USERNAME = "alice_spike"
# A nonce makes our own message unmistakable in the received stream.
NONCE = os.environ.get("SPIKE_NONCE", "nonce-PLACEHOLDER")
SEND_BODY = f"hello from the spike [{NONCE}]"

_ACTOR_NAME = "aiko_spike"
_PROTOCOL = f"{aiko.SERVICE_PROTOCOL_AIKO}/{_ACTOR_NAME}:0"

# Collected on the aiko event-loop thread; read on the main thread.
_received: list[dict] = []
_received_lock = threading.Lock()


class SpikeActor(aiko.Actor):
    """Headless client: discover ChatServer, subscribe to the channel, send."""

    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.chat_server = None
        self.server_topic_path = None
        self.channel_topic = None
        aiko.do_discovery(
            ChatServer,
            get_server_service_filter(),
            self._discovery_add,
            self._discovery_remove,
        )

    def _discovery_add(self, service_details, service):
        self.server_topic_path = service_details[0]
        self.chat_server = service
        self.channel_topic = f"{service_details[0]}/{CHANNEL}"
        self.add_message_handler(self._on_payload, self.channel_topic)
        print(f"[spike] discovered ChatServer {service_details[1]!r} "
              f"topic_path={self.server_topic_path!r} "
              f"subscribed={self.channel_topic!r}", flush=True)

    def _discovery_remove(self, service_details):
        print(f"[spike] ChatServer disconnected: {service_details[1]!r}", flush=True)
        self.chat_server = None

    def _on_payload(self, _aiko, topic, payload_in):
        # Fires on the aiko event-loop thread for every payload on the channel.
        rec = {"topic": topic, "raw": payload_in, "recv_at": time.time()}
        try:
            rec["parsed"] = json.loads(payload_in)
        except (TypeError, ValueError):
            rec["parsed"] = None  # legacy bare-string payload
        with _received_lock:
            _received.append(rec)
        print(f"[spike] RECEIVED on {topic!r}: {payload_in!r}", flush=True)

    def send(self, username: str, text: str) -> bool:
        if self.chat_server is None:
            return False
        self.chat_server.send_message(username, [CHANNEL], text)
        return True


class SpikeClient:
    def __init__(self):
        self._actor: SpikeActor | None = None

    def run_forever(self):
        init_args = aiko.actor_args(_ACTOR_NAME, protocol=_PROTOCOL, tags=["ec=true"])
        self._actor = aiko.compose_instance(SpikeActor, init_args)
        print("[spike] aiko event loop starting", flush=True)
        aiko.process.run()  # blocking

    def actor(self) -> SpikeActor | None:
        return self._actor

    def stop(self) -> None:
        try:
            aiko.process.terminate()
        except Exception:
            pass


def _verdict():
    """Analyse what came back and print the GATE answer."""
    with _received_lock:
        recs = list(_received)

    own = [r for r in recs if NONCE in (r["raw"] or "")]
    print("\n" + "=" * 70, flush=True)
    print("PHASE 0 GATE — aiko echo semantics", flush=True)
    print("=" * 70, flush=True)
    print(f"total payloads received on channel topic: {len(recs)}", flush=True)
    print(f"payloads containing our nonce ({NONCE}): {len(own)}", flush=True)

    echoed = len(own) > 0
    print(f"\n[Q1] Does our own publish come back on the subscribed topic?  "
          f"{'YES (echo present)' if echoed else 'NO (no echo)'}", flush=True)

    username_exact = None
    payload_shape = None
    if own:
        sample = own[0]
        payload_shape = sample["parsed"]
        if isinstance(sample["parsed"], dict):
            got_user = sample["parsed"].get("username")
            username_exact = (got_user == SEND_AS_USERNAME)
            print(f"[Q2] Is `username` preserved byte-exact?  "
                  f"sent={SEND_AS_USERNAME!r} got={got_user!r}  "
                  f"-> {'YES' if username_exact else 'NO'}", flush=True)
            print(f"[Q3] Inbound payload shape: keys={sorted(sample['parsed'].keys())}", flush=True)
            print(f"      full sample: {json.dumps(sample['parsed'])}", flush=True)
        else:
            print(f"[Q2] Payload was NOT JSON (legacy bare string): {sample['raw']!r}", flush=True)

    print("\nDESIGN IMPLICATION:", flush=True)
    if echoed and username_exact:
        print("  -> Echo IS present AND username is byte-exact.", flush=True)
        print("  -> redis dedupe key (channel, username, body_hash) is VIABLE.", flush=True)
        print("  -> Build echo-suppression Layer A as planned (plan §A5).", flush=True)
    elif echoed and username_exact is False:
        print("  -> Echo present but username NOT preserved.", flush=True)
        print("  -> Dedupe must drop username from the key -> (channel, body_hash, window).", flush=True)
    elif not echoed:
        print("  -> No echo. Echo suppression (Layer A) is UNNECESSARY. Simpler ingest.", flush=True)
    else:
        print("  -> Inconclusive / non-JSON. Inspect output above.", flush=True)
    print("=" * 70 + "\n", flush=True)

    # Exit code encodes the gate result for the orchestration script.
    return 0 if echoed and username_exact else (3 if not echoed else 4)


def main():
    client = SpikeClient()
    t = threading.Thread(target=client.run_forever, daemon=True)
    t.start()

    # Wait for discovery (ChatServer found + proxy ready), up to 20s.
    deadline = time.time() + 20
    actor = None
    while time.time() < deadline:
        actor = client.actor()
        if actor is not None and actor.chat_server is not None:
            break
        time.sleep(0.2)

    if actor is None or actor.chat_server is None:
        print("[spike] FAILED: ChatServer never discovered within 20s. "
              "Is aiko_registrar + aiko_chat running against the same broker?",
              flush=True)
        os._exit(2)

    # Give the subscription a beat to settle before we publish.
    time.sleep(1.0)
    print(f"[spike] sending as {SEND_AS_USERNAME!r}: {SEND_BODY!r}", flush=True)
    actor.send(SEND_AS_USERNAME, SEND_BODY)

    # Collect echoes for a few seconds.
    time.sleep(4.0)

    code = _verdict()
    client.stop()
    os._exit(code)


if __name__ == "__main__":
    main()
