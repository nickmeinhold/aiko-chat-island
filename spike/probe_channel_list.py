"""SPIKE (#1281 incr 2): prove the gateway can read canonical channel topology
from aiko's ChatServer via an EC share consumer — no upstream edits.

What it does: composes a minimal aiko Actor that discovers the running
ChatServer (same `do_discovery(ChatServer, ...)` the gateway already uses),
then attaches an `ECConsumer(filter="channel_list")` to the discovered
producer's `<topic>/control`. The consumer maintains a live local cache that
the producer populates with `add` commands for every channel entry.

Expected on the devstack: the cache fills with the 5 bootstrapped channels
(general random llm robot yolo) — proving the read-through mechanism end-to-end,
including that the share is actually POPULATED at runtime (not an empty dict set
before the HyperSpace category loaded from storage).

Run AFTER `spike/devstack.sh up`. Prints the observed cache + add-events and
exits 0 iff at least the bootstrapped channels arrive.
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("AIKO_MQTT_HOST", "localhost")
os.environ.setdefault("AIKO_MQTT_PORT", "1884")
os.environ.setdefault("AIKO_NAMESPACE", "aiko")

import aiko_services as aiko  # noqa: E402  (env must be set before import)
from aiko_chat.chat import ChatServer, get_server_service_filter  # noqa: E402

EXPECTED = {"general", "random", "llm", "robot", "yolo"}

_state = {
    "discovered": False,
    "topic_path": None,
    "cache": None,
    "events": [],  # (command, item_name, item_value)
}


class ProbeActor(aiko.Actor):
    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.chat_server = None
        self.server_topic_path = None
        self.ec_consumer = None
        self.cache: dict = {}
        aiko.do_discovery(
            ChatServer,
            get_server_service_filter(),
            self._discovery_add,
            self._discovery_remove,
        )

    def _discovery_add(self, service_details, service):
        self.server_topic_path = service_details[0]
        self.chat_server = service
        _state["discovered"] = True
        _state["topic_path"] = self.server_topic_path
        _state["cache"] = self.cache
        # The exact dashboard pattern: topic_control = "<topic_path>/control".
        topic_control = f"{self.server_topic_path}/control"
        # filter="channel_list" => producer sends only the channel_list subtree.
        self.ec_consumer = aiko.ECConsumer(
            self, 0, self.cache, topic_control, filter="channel_list"
        )
        self.ec_consumer.add_handler(self._on_share_event)
        print(f"[probe] discovered ChatServer at {self.server_topic_path}; "
              f"attached ECConsumer(filter='channel_list')", flush=True)

    def _on_share_event(self, consumer_id, command, item_name, item_value):
        _state["events"].append((command, item_name, item_value))
        print(f"[probe] share event: {command} {item_name} = {item_value!r}",
              flush=True)

    def _discovery_remove(self, service_details):
        print("[probe] ChatServer disconnected", flush=True)


def _run_aiko():
    init_args = aiko.actor_args("probe_channel_list",
                                protocol=f"{aiko.SERVICE_PROTOCOL_AIKO}/probe:0",
                                tags=["ec=true"])
    aiko.compose_instance(ProbeActor, init_args)
    aiko.process.run()  # blocking


def main() -> int:
    t = threading.Thread(target=_run_aiko, name="aiko-probe", daemon=True)
    t.start()

    deadline = time.time() + 25
    while time.time() < deadline and not _state["discovered"]:
        time.sleep(0.2)
    if not _state["discovered"]:
        print("[probe] FAIL: never discovered ChatServer within 25s", flush=True)
        return 2

    # Let the share request -> synchronize -> add commands settle.
    time.sleep(5)

    cache = _state["cache"] or {}
    channel_list = cache.get("channel_list", {})
    observed = set(channel_list.keys()) if isinstance(channel_list, dict) else set()

    print("\n[probe] ===== RESULT =====", flush=True)
    print(f"[probe] raw cache keys: {list(cache.keys())}", flush=True)
    print(f"[probe] channel_list entries: {sorted(observed)}", flush=True)
    print(f"[probe] add-events: {len(_state['events'])}", flush=True)

    try:
        aiko.process.terminate()
    except Exception:
        pass

    if EXPECTED <= observed:
        print(f"[probe] PASS: all {len(EXPECTED)} bootstrapped channels arrived "
              f"via the EC share.", flush=True)
        return 0
    print(f"[probe] PARTIAL/FAIL: expected superset {sorted(EXPECTED)}, "
          f"observed {sorted(observed)}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
