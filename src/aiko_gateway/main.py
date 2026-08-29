"""FastAPI application — the gateway spine.

Boots the aiko bus client, mirrors channel topology off the bus (an ECConsumer
on ChatServer's ``channel_list`` share, drained through one ordered FIFO worker),
persists every observed message into its channel (local SQLite — the store for
data HyperSpace cannot hold), and serves the durable HTTP/WS contract: auth
(password, social sign-in, OAuth broker, passkeys), channels + history,
messages, communities, members, moderation, devices, and the island directory.
Bus threads hop onto the asyncio loop via ``call_soon_threadsafe``; the gateway
suppresses its own echoes so a send isn't persisted twice.

See ``README.md`` for architecture and ``docs/design/`` for the design record.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, status

# NOTE: `AikoBusClient` is imported lazily inside `lifespan` (not at module
# scope) so that `import aiko_gateway.main` does NOT transitively pull in
# `aiko_services` (an undeclared dependency absent on clean CI). This keeps the
# production app importable under the test suite's "never import aiko_services"
# isolation invariant, which lets a test introspect the real route table +
# auth dependency tree without the bus. See tests/test_main_routes.py.
from .aiko.payload import InboundMessage

if TYPE_CHECKING:
    from .aiko.client import AikoBusClient
from .config import settings
from .db import SessionLocal, verify_schema
from .worker_guard import acquire_single_worker_lock, release_single_worker_lock
from .domain import (apns, channels_service, echo, messages_service,
                     moderation_service)
from .realtime.hub import Hub

logging.basicConfig(level=logging.INFO)
# Redact credential-shaped hex from EVERY emitted record, not just httpx's
# (claude-tasks#3586). Must run after basicConfig — it attaches to the root
# HANDLERS, which do not exist until then, and a handler filter is the only kind
# that sees records from loggers nobody anticipated. Idempotent.
apns.install_log_redaction()
log = logging.getLogger("aiko_gateway")

settings.export_aiko_env()  # aiko_services reads AIKO_MQTT_* from os.environ


class GatewayState:
    def __init__(self) -> None:
        self.bus: "AikoBusClient | None" = None
        self.hub: Hub = Hub()
        self.loop: asyncio.AbstractEventLoop | None = None
        # Single ordered lane for channel topology events (set up in lifespan
        # once the loop exists). One worker drains it FIFO so an add/remove pair
        # for the same channel can never interleave (cage-match PR#12, P1a).
        self._channel_events: "asyncio.Queue[tuple[str, str]] | None" = None
        self._channel_worker: "asyncio.Task | None" = None

    def on_bus_message(self, msg: InboundMessage) -> None:
        """AIKO thread -> hop to the asyncio loop, then ingest."""
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(self._ingest(msg))
        )

    async def _ingest(self, msg: InboundMessage) -> None:
        # Drop our own echo — it was already persisted + fanned out at send-time.
        if msg.channel and echo.is_own_echo(msg.channel, msg.username, msg.message):
            return
        try:
            async with SessionLocal() as session:
                row = await messages_service.persist_inbound(session, msg)
                # Block exclusion (#7): a bus message can map to a real gateway
                # user (sender_user_id set) — that human's blocks apply to live
                # delivery just as on the send path. An external actor (NULL
                # sender) has no block relationships, so the set is empty.
                exclude = (
                    await moderation_service.blocked_pair_user_ids(session, row.sender_user_id)
                    if row and row.sender_user_id else set()
                )
            if row:
                log.info("ingest %s in %s: %s", row.id, msg.channel, msg.message)
                # External message (LLM/robot/REPL/other) -> fan out live.
                await self.hub.fanout(
                    row.channel_id,
                    {"type": "message", "msg": messages_service.message_view(row)},
                    exclude_user_ids=exclude,
                )
        except Exception:
            log.exception("ingest failed for bus message")

    # -- channel topology reconcile (#1281 incr 2) --------------------------- #
    # The aiko ChatServer's `channel_list` EC share is the canonical source of
    # channel existence. Callbacks fire on the AIKO thread and enqueue onto a
    # SINGLE ordered asyncio queue; one worker drains it FIFO and reconciles
    # into the local Channel rows. The queue is what makes ordering safe — a bare
    # `create_task` per event let an add/remove pair for the same channel
    # interleave at the DB await and finish out of order, which for an
    # irreversible hard-delete is unacceptable (cage-match PR#12, Carnot P1a).
    # Replaces the old independent `_seed_channels`.

    def on_channel_add(self, aiko_channel: str) -> None:
        self._enqueue_channel_event("add", aiko_channel)

    def on_channel_remove(self, aiko_channel: str) -> None:
        self._enqueue_channel_event("remove", aiko_channel)

    def _enqueue_channel_event(self, action: str, aiko_channel: str) -> None:
        """AIKO thread -> the one ordered topology queue (thread-safe handoff)."""
        if self.loop is None or self._channel_events is None:
            return
        self.loop.call_soon_threadsafe(
            self._channel_events.put_nowait, (action, aiko_channel)
        )

    async def _run_channel_worker(self) -> None:
        """Single consumer of the topology queue — serializes every add/remove so
        they apply in arrival order. Owns the transaction boundary (the services
        only flush)."""
        assert self._channel_events is not None
        while True:
            action, aiko_channel = await self._channel_events.get()
            try:
                async with SessionLocal() as session:
                    if action == "add":
                        await channels_service.upsert_channel(session, aiko_channel)
                        await session.commit()
                        log.info("channel reconcile: + %s", aiko_channel)
                    elif action == "remove":
                        deleted = await channels_service.hard_delete_channel(
                            session, aiko_channel)
                        await session.commit()
                        if deleted:
                            log.warning(
                                "channel reconcile: HARD-DELETED %s "
                                "(+ its messages + memberships)", aiko_channel)
            except channels_service.ReservedDmChannel:
                # A bus channel_list named a reserved dm: channel — anomalous (a DM never
                # federates). Skip cleanly (not a stack-trace error): the dm: namespace is
                # reserved for island-local DMs (#2633, cage-match PR#124 Tesla).
                log.warning("channel reconcile: refused reserved dm: name %s (%s)",
                            aiko_channel, action)
            except Exception:
                log.exception("channel reconcile failed: %s %s", action, aiko_channel)
            finally:
                self._channel_events.task_done()


state = GatewayState()


async def _gossip_loop(interval: int) -> None:
    """Background anti-entropy for the island directory (#1546). Pulls each known
    peer's island directory (/v1/islands, falling back to the deprecated /v1/gateways
    for pre-taxonomy peers) and merges, immediately then every `interval` seconds, so
    peers converge with no central registry. Best-effort: a failing round is logged
    and retried, never fatal. Cancelled cleanly on shutdown."""
    import httpx

    from .domain.peers_service import directory, gossip_once
    async with httpx.AsyncClient(follow_redirects=False) as client:
        while True:
            try:
                await gossip_once(directory, client)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("gossip loop iteration failed")
            await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.loop = asyncio.get_running_loop()
    # Enforce single-worker serving BEFORE any serving side-effect: the realtime Hub
    # is per-process, so a ban/disconnect can't reach sockets on another worker until
    # the #46 cross-worker sweep lands. A second worker fails to take the lock and
    # refuses to boot (GATEWAY_ALLOW_MULTIWORKER=true lifts it). See worker_guard.
    acquire_single_worker_lock()
    # Everything after acquire is wrapped so the lock is released on ANY exit path,
    # including a startup failure BEFORE `yield` (verify_schema / bus.start raising)
    # or an exception during cleanup — otherwise the fd (and the lock) would leak in
    # a non-exiting host (a test harness / embedded server). Kelvin+Carnot, PR#111.
    gossip_task: "asyncio.Task | None" = None
    try:
        # Alembic (run by the container entrypoint before uvicorn) owns schema
        # creation/evolution; here we only VERIFY the live schema is migrated +
        # current, failing closed if not (#14).
        await verify_schema()
        # Say it at boot if this island is holding device tokens it cannot reach
        # (#3397). Silent unless there is something wrong — see warn_if_unreachable.
        # Opens its own session: the request-scoped dependency does not exist yet.
        from .domain import push_service as _push  # lazy: see the aclose() note
        async with SessionLocal() as _session:
            await _push.warn_if_unreachable(_session)
        # No independent seeding: channels are reconciled from the ChatServer
        # `channel_list` EC share once the bus client discovers it. An inbound
        # message for a not-yet-reconciled channel is upserted by persist_inbound
        # (closes the startup window). See #1281 incr 2.
        state._channel_events = asyncio.Queue()
        state._channel_worker = asyncio.create_task(state._run_channel_worker())
        # Lazy import: pulling aiko_services happens only at startup, never at
        # module import time (see the import-block note above).
        from .aiko.client import AikoBusClient
        state.bus = AikoBusClient(
            settings.aiko_channels, state.on_bus_message,
            on_channel_add=state.on_channel_add,
            on_channel_remove=state.on_channel_remove,
        )
        state.bus.start()
        log.info("Gateway started; subscribed channels=%s (topology via channel_list share)",
                 settings.aiko_channels)
        # Gateway directory gossip (#1546): converge the known-peer set by anti-entropy.
        # FAIL-CLOSED: the fetch path is an SSRF surface (#1578), so it runs ONLY when
        # explicitly enabled. Off by default, the directory still serves self +
        # seed_peers with no network fetch — the safe operator-curated path.
        gi = settings.gateway_gossip_interval_seconds
        if settings.gateway_gossip_enabled and gi > 0:
            gossip_task = asyncio.create_task(_gossip_loop(gi))
            log.info("gateway directory gossip ENABLED (interval=%ds, bootstrap_peers=%d)",
                     gi, len(settings.gateway_bootstrap_peers))
        else:
            log.info("gateway directory gossip disabled; serving self + %d seed peer(s)",
                     len(settings.gateway_seed_peers))
        try:
            yield
        finally:
            if state.bus is not None:
                state.bus.stop()
            if state._channel_worker is not None:
                state._channel_worker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await state._channel_worker  # clean task ownership (Carnot r2)
            if gossip_task is not None:
                gossip_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await gossip_task
            # Push shutdown, IN THIS ORDER (#3267; cage-match #139 Maxwell+Carnot).
            # DRAIN the in-flight wake tasks FIRST, THEN close the pooled APNs
            # HTTP/2 connection — closing the shared client while a wake is
            # mid-send tears the connection out from under it, and the resulting
            # error is swallowed by wake_for_message's broad except as a
            # misleading "wake failed". The ordering IS the fix; do not swap
            # these two lines. Both are no-ops on an island with push
            # unconfigured (every island today). Imported here rather than at
            # module scope to keep the import graph of `main` unchanged for the
            # clean-checkout route-table tests.
            from .domain import apns, push_service
            await push_service.aclose()
            await apns.aclose()
    finally:
        # Outermost: runs whether startup raised before yield or cleanup raised.
        release_single_worker_lock()


app = FastAPI(title="Aiko Chat Gateway", version="0.0.1", lifespan=lifespan)
app.state.gw = state  # the WS endpoint reaches bus + hub via websocket.app.state.gw

# Cap request body size app-wide (#28) — rejects oversized bodies with 413 before
# they reach a route. Generous cap (no upload endpoint); see middleware + config.
from .middleware import ContentSizeLimitMiddleware  # noqa: E402
app.add_middleware(ContentSizeLimitMiddleware, max_bytes=settings.max_request_bytes)

from .rest.deps import DbSession  # noqa: E402
from .rest import auth as auth_routes  # noqa: E402
from .rest import channels as channel_routes  # noqa: E402
from .rest import communities as community_routes  # noqa: E402
from .rest import devices as device_routes  # noqa: E402
from .rest import dm as dm_routes  # noqa: E402
from .rest import island as island_self_routes  # noqa: E402
from .rest import islands as island_routes  # noqa: E402
from .rest import keys as key_routes  # noqa: E402
from .rest import legal as legal_routes  # noqa: E402
from .rest import livekit as livekit_routes  # noqa: E402
from .rest import members as member_routes  # noqa: E402
from .rest import messages as message_routes  # noqa: E402
from .rest import reactions as reaction_routes  # noqa: E402
from .rest import moderation as moderation_routes  # noqa: E402
from .rest import recovery as recovery_routes  # noqa: E402
from .rest import well_known as well_known_routes  # noqa: E402
from .realtime import ws as ws_routes  # noqa: E402
from .rest.errors import register_error_handlers  # noqa: E402
register_error_handlers(app)  # structured ban-403 body (single door, mirrors tests)
app.include_router(auth_routes.router)
app.include_router(auth_routes.me_router)
app.include_router(channel_routes.router)
app.include_router(community_routes.router)
app.include_router(device_routes.router)
app.include_router(dm_routes.router)
app.include_router(island_self_routes.router)
app.include_router(island_routes.router)
app.include_router(key_routes.router)
app.include_router(legal_routes.router)
app.include_router(livekit_routes.router)
app.include_router(member_routes.router)
app.include_router(message_routes.router)
app.include_router(moderation_routes.router)
app.include_router(reaction_routes.router)
app.include_router(recovery_routes.router)
app.include_router(ws_routes.router)
app.include_router(well_known_routes.router)


async def _reachability(session) -> dict:
    """The `push` block for /health — DEGRADES rather than failing the endpoint.

    Lazy import so `main`'s module-level graph stays exactly as it was: the
    clean-checkout route-table tests introspect the real app without
    `aiko_services` installed, and the header note asks that this graph not grow.
    sys.modules caches after the first call, so the cost is a dict lookup.

    NEVER RAISES, and that is the whole point of this wrapper. Before #3397 this
    endpoint touched no database, so a DB problem could not affect it. It is also
    the container's liveness probe (`curl -fsS /health`, compose healthcheck) AND
    deploy/update.sh's post-deploy verification — so a raising /health turns a
    transient SQLite lock into a container marked unhealthy and a SUCCESSFUL
    deploy reported as a failure. Adding a subsystem dependency to a liveness
    probe is how a dependency blip becomes an outage; the new information must
    ride along without inheriting that power.

    BOOLEANS, NOT COUNTS, on this endpoint specifically: /health is public and
    unauthenticated (it already exposes `channels`, which is config). A live
    device population is user-adjacent data and does not belong on it — this
    island's grain is not leaking facts about its people. The COUNT, which is
    what an operator acts on, goes to the boot log where box access is the
    prerequisite. Tradeoff named rather than absorbed: a monitor scraping
    /health learns THAT devices are unreachable, not how many.
    """
    from .domain import push_service
    try:
        report = await push_service.reachability(session)
    except Exception:  # pragma: no cover - exercised via the raising-session test
        # Unknown, not false: reporting `configured: false` here would invent an
        # alarm out of a database hiccup, and a false alarm costs more than a
        # missing one on a field that is advisory.
        log.warning("/health could not read push reachability", exc_info=True)
        return {"status": "unknown"}
    return {
        "configured": report["configured"],
        "devices_unreachable": bool(report["unreachable_devices"]),
    }


@app.get("/health")
async def health(session: DbSession) -> dict:
    """Liveness plus the two things that are silently wrong more often than the
    process is down.

    `push` is READ LIVE on every call, never cached at boot (#3397). The standing
    complaint about this endpoint is that it reports config INTENT rather than
    reality (#3193) — a boot-time snapshot would answer "at startup" while the
    operator is asking "now", and a device registered five minutes ago is exactly
    the case worth surfacing. It costs one COUNT against a table that holds single
    digits, which is cheaper than the four hours the silence cost once.
    """
    return {
        "status": "ok",
        "aiko_connected": bool(state.bus and state.bus.connected),
        "channels": settings.aiko_channels,
        "push": await _reachability(session),
    }


@app.post("/v1/_debug/send")
def debug_send(channel: str, username: str, message: str) -> dict:
    """Dev-only (pre-auth): publish onto the bus. The echo is what gets persisted.

    Gated to non-production: this endpoint has NO auth and would let anyone
    inject bus messages, so it 404s in production (fail-closed alongside the
    jwt_secret + registration guards)."""
    if settings.is_production:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    ok = bool(state.bus and state.bus.send(username, channel, message))
    return {"sent": ok}
