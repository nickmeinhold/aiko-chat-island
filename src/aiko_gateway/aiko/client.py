"""The aiko (bus) side of the gateway — a headless multi-channel client.

Generalised from aiko-chat-bridge/aiko_bridge/aiko_client.py (the proven
pattern). Differences from the Matrix bridge:
  * subscribes to MANY channels, not one (the gateway bridges every mapped
    aiko channel), and
  * the inbound callback receives a parsed InboundMessage with the channel it
    arrived on, so the gateway can route to the right persisted channel.

Threading: a single BridgeChatActor owns aiko's blocking event loop on a
dedicated daemon thread. `send()` is the only method called from the asyncio
side; it bottoms out in a thread-safe paho publish. The asyncio<->aiko bridge
(run_coroutine_threadsafe) lives one layer up, in the FastAPI lifespan.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable

import aiko_services as aiko
from aiko_chat.chat import ChatServer, get_server_service_filter

from .payload import InboundMessage, parse_payload

log = logging.getLogger("aiko_gateway.aiko")

_ACTOR_NAME = "aiko_gateway"
_PROTOCOL = f"{aiko.SERVICE_PROTOCOL_AIKO}/{_ACTOR_NAME}:0"

# Callback invoked (on the aiko thread) for every inbound channel message.
OnMessage = Callable[[InboundMessage], None]


class GatewayChatActor(aiko.Actor):
    """Discovers the ChatServer, subscribes to each mapped channel, relays."""

    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.chat_server = None
        self.server_topic_path: str | None = None
        # Set by AikoBusClient before the event loop starts:
        self.channels: list[str] = ["general"]
        self.on_message: OnMessage | None = None
        self._subscribed: dict[str, str] = {}  # channel -> topic

        aiko.do_discovery(
            ChatServer,
            get_server_service_filter(),
            self._discovery_add,
            self._discovery_remove,
        )

    # -- discovery ---------------------------------------------------------
    def _discovery_add(self, service_details, service):
        self.server_topic_path = service_details[0]
        self.chat_server = service
        for channel in self.channels:
            topic = f"{self.server_topic_path}/{channel}"
            self.add_message_handler(self._on_payload, topic)
            self._subscribed[channel] = topic
        log.info("Connected to ChatServer %s; subscribed channels=%s",
                 service_details[1], list(self._subscribed))

    def _discovery_remove(self, service_details):
        log.warning("ChatServer %s disconnected", service_details[1])
        self.chat_server = None
        self.server_topic_path = None
        self._subscribed.clear()

    # -- inbound (bus -> gateway) -----------------------------------------
    def _on_payload(self, _aiko, topic, payload_in):
        channel = topic.rsplit("/", 1)[-1]
        msg = parse_payload(payload_in, fallback_channel=channel)
        if self.on_message is not None:
            try:
                self.on_message(msg)
            except Exception:  # never let a handler kill the aiko loop
                log.exception("on_message handler raised")

    # -- outbound (gateway -> bus) ----------------------------------------
    def send(self, username: str, channel: str, text: str) -> bool:
        if self.chat_server is None:
            log.warning("Dropping outbound; no ChatServer discovered yet")
            return False
        # geekscape signature: send_message(username, recipients, message)
        self.chat_server.send_message(username, [channel], text)
        return True


class AikoBusClient:
    """Owns the aiko Actor + the blocking aiko event loop on a daemon thread."""

    def __init__(self, channels: list[str], on_message: OnMessage):
        self._channels = channels
        self._on_message = on_message
        self._actor: GatewayChatActor | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="aiko-bus", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        init_args = aiko.actor_args(_ACTOR_NAME, protocol=_PROTOCOL, tags=["ec=true"])
        self._actor = aiko.compose_instance(GatewayChatActor, init_args)
        self._actor.channels = self._channels
        self._actor.on_message = self._on_message
        log.info("Starting aiko event loop (channels=%s)", self._channels)
        aiko.process.run()  # blocking

    @property
    def connected(self) -> bool:
        return self._actor is not None and self._actor.chat_server is not None

    def send(self, username: str, channel: str, text: str) -> bool:
        if self._actor is None:
            return False
        return self._actor.send(username, channel, text)

    def stop(self) -> None:
        try:
            aiko.process.terminate()
        except Exception:
            log.exception("Error terminating aiko process")
