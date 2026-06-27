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

# channels_service imports only models/sqlalchemy (no aiko_services), so pulling
# it here does not break this module's lazy-import isolation.
from ..domain import channels_service
from .payload import InboundMessage, parse_payload

log = logging.getLogger("aiko_gateway.aiko")

_ACTOR_NAME = "aiko_gateway"
_PROTOCOL = f"{aiko.SERVICE_PROTOCOL_AIKO}/{_ACTOR_NAME}:0"

# Callback invoked (on the aiko thread) for every inbound channel message.
OnMessage = Callable[[InboundMessage], None]
# Callback invoked (on the aiko thread) with a channel name for topology events.
OnChannelEvent = Callable[[str], None]


class GatewayChatActor(aiko.Actor):
    """Discovers the ChatServer, subscribes to each mapped channel, relays."""

    def __init__(self, context):
        context.call_init(self, "Actor", context)
        self.chat_server = None
        self.server_topic_path: str | None = None
        # Set by AikoBusClient before the event loop starts:
        self.channels: list[str] = ["general"]
        self.on_message: OnMessage | None = None
        self.on_channel_add: OnChannelEvent | None = None
        self.on_channel_remove: OnChannelEvent | None = None
        self._subscribed: dict[str, str] = {}  # channel -> topic
        self._ec_consumer = None
        self._channel_cache: dict = {}

        aiko.do_discovery(
            ChatServer,
            get_server_service_filter(),
            self._discovery_add,
            self._discovery_remove,
        )

    # -- discovery ---------------------------------------------------------
    def _discovery_add(self, service_details, service):
        # aiko gives no contract that `add` is always preceded by `remove`; a
        # re-add without an intervening remove would overwrite _ec_consumer and
        # leak the old one's handler + lease (cage-match PR#12, Carnot P2a).
        # Tear down any prior attachment first so _discovery_add is idempotent.
        self._teardown_attachments()
        self.server_topic_path = service_details[0]
        self.chat_server = service
        # Bootstrap floor: always relay the configured channel(s) immediately so
        # there's no gap before the channel_list share replays. The share's add
        # for these is then a no-op (idempotent). Every OTHER channel is
        # subscribed dynamically from the channel_list share (#1281 incr 2 →
        # #8): see _on_share_event.
        for channel in self.channels:
            self._subscribe_channel(channel)
        # Channel topology read-through (#1281 incr 2): mirror the ChatServer's
        # canonical `channel_list` EC share. A fresh consumer per producer
        # instance — on a ChatServer restart this rebinds to the new topic_path
        # and re-receives `add`s (validated: spike/probe_restart_removes.py).
        self._channel_cache = {}
        self._ec_consumer = aiko.ECConsumer(
            self, 0, self._channel_cache,
            f"{self.server_topic_path}/control", filter="channel_list",
        )
        self._ec_consumer.add_handler(self._on_share_event)
        log.info("Connected to ChatServer %s; subscribed channels=%s; "
                 "channel_list reconcile attached",
                 service_details[1], list(self._subscribed))

    def _discovery_remove(self, service_details):
        log.warning("ChatServer %s disconnected", service_details[1])
        self.chat_server = None
        self.server_topic_path = None
        # Tear down attachments but DO NOT signal a channel removal: a disconnect
        # is transient, not a real upstream removal (Decision B).
        self._teardown_attachments()

    def _teardown_attachments(self) -> None:
        """Symmetric detach for everything _discovery_add wires up: the payload
        message handlers AND the channel_list EC consumer. Pre-PR the payload
        handlers were never removed (Carnot P2b) — now that a second
        lifecycle-managed attachment exists, both are cleaned up together."""
        for topic in self._subscribed.values():
            try:
                self.remove_message_handler(self._on_payload, topic)
            except Exception:
                log.exception("Error removing payload handler for %s", topic)
        self._subscribed.clear()
        if self._ec_consumer is not None:
            try:
                self._ec_consumer.terminate()
            except Exception:
                log.exception("Error terminating channel_list consumer")
            self._ec_consumer = None
        self._channel_cache = {}

    # -- dynamic per-channel subscription (aiko thread only) ---------------
    # add_message_handler / remove_message_handler are aiko Actor operations and
    # MUST run on the aiko thread. Every caller below (_discovery_add,
    # _on_share_event, _teardown_attachments) is on that thread, so `_subscribed`
    # is single-threaded state — no lock needed. The DB reconcile is the separate
    # asyncio-side concern (main.py's ordered worker); subscription is purely the
    # bus-side concern and lives here.
    def _subscribe_channel(self, channel: str) -> None:
        """Idempotently relay messages for `channel`. No-op if already subscribed
        or the server isn't attached yet."""
        if channel in self._subscribed or self.server_topic_path is None:
            return
        topic = f"{self.server_topic_path}/{channel}"
        self.add_message_handler(self._on_payload, topic)
        self._subscribed[channel] = topic
        log.info("subscribed channel relay: %s", channel)

    def _unsubscribe_channel(self, channel: str) -> None:
        """Stop relaying `channel`. No-op if not subscribed."""
        topic = self._subscribed.pop(channel, None)
        if topic is None:
            return
        try:
            self.remove_message_handler(self._on_payload, topic)
        except Exception:
            log.exception("Error removing payload handler for %s", topic)
        log.info("unsubscribed channel relay: %s", channel)

    # -- channel topology (bus share -> reconcile) ------------------------
    def _on_share_event(self, _consumer_id, command, item_name, _item_value):
        """ECConsumer handler (aiko thread). Translate a channel_list add/remove
        into (1) the bus subscription for message relay AND (2) the DB topology
        callback. `add`/`update` => existence (idempotent upsert + subscribe);
        `remove` => the ONLY hard-delete trigger (+ unsubscribe). Non-channel
        items (the bare `channel_list` parent, other keys) are ignored.

        Ordering matters per direction:
          * add: SUBSCRIBE first, then signal the DB upsert. A message arriving
            in the gap is caught by persist_inbound's upsert (the existing
            safety net) — no relay lost.
          * remove: UNSUBSCRIBE first, then signal the DB delete. Stops new
            messages BEFORE the row is deleted, so none can re-mint the channel
            via persist_inbound after the delete (the #6 drift vector)."""
        name = channels_service.channel_name_from_item(item_name)
        if name is None:
            return
        try:
            if command in ("add", "update"):
                self._subscribe_channel(name)
                if self.on_channel_add is not None:
                    self.on_channel_add(name)
            elif command == "remove":
                self._unsubscribe_channel(name)
                if self.on_channel_remove is not None:
                    self.on_channel_remove(name)
        except Exception:  # never let a handler kill the aiko loop
            log.exception("channel share handler raised")

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

    def __init__(self, channels: list[str], on_message: OnMessage,
                 on_channel_add: OnChannelEvent | None = None,
                 on_channel_remove: OnChannelEvent | None = None):
        self._channels = channels
        self._on_message = on_message
        self._on_channel_add = on_channel_add
        self._on_channel_remove = on_channel_remove
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
        self._actor.on_channel_add = self._on_channel_add
        self._actor.on_channel_remove = self._on_channel_remove
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
