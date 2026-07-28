"""UGC moderation endpoints — block / unblock / list-blocks + report (#7).

The REST surface Apple 1.2 / Google UGC require: a user can BLOCK another user
(and stop seeing their content — enforced in the read + fanout paths) and REPORT
an objectionable message (feeding the ops queue behind the EULA's 24h-action
commitment).

I1 (auth): every route takes ``CurrentUser`` so an unauthenticated caller is
rejected before any row is touched. The trust-boundary logic lives in
``moderation_service`` (single enforcement source, mirroring ``acl`` /
``memberships_service``); this layer only translates the service's typed
rejections into HTTP. Report resolves the target through the channel ACL FIRST so
a user can only report a message they can actually see (and so an unseeable
private-channel message stays existence-hidden behind the same 404).
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Literal
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from pydantic import BaseModel

from ..config import settings
from ..domain import moderation_service
from ..realtime import envelopes
from .deps import CurrentUser, DbSession, ModeratorUser

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["moderation"])

# Operator alert (Piece A): the reported-message body preview is truncated to this
# many chars in the webhook payload — enough for triage, not a content dump.
_ALERT_PREVIEW_MAX = 120


async def _deliver_moderation_alert(url: str, payload: dict) -> None:
    """Best-effort operator ping when a report lands. Fire-and-forget: any failure
    (timeout, DNS, non-2xx) is swallowed with a HOST-ONLY warning so a broken
    webhook never affects the report write. The destination comes solely from
    operator config (``settings.moderation_alert_webhook_url``), never a request
    value — so there is no SSRF surface; only the payload carries user content. We
    log the exception TYPE and the URL host only (never ``str(exc)`` or the full
    URL) so a token embedded in either can't leak into logs."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — best-effort; must never propagate
        host = urlparse(url).hostname or "?"
        log.warning("moderation alert webhook failed (host=%s): %s",
                    host, type(exc).__name__)


# Closed set, mirrors moderation_service.REPORT_REASONS — an unknown reason is a
# 422 at the boundary, never a free-text blob in the column.
ReportReason = Enum("ReportReason", {r: r for r in moderation_service.REPORT_REASONS}, type=str)


class ReportReq(BaseModel):
    reason: ReportReason


@router.post("/users/{user_id}/block", status_code=status.HTTP_204_NO_CONTENT)
async def block_user(user_id: str, user: CurrentUser, session: DbSession) -> None:
    """Block ``user_id`` for the caller. Idempotent (re-block is a no-op).
    Mutual effect: neither party sees the other's messages nor may reply to
    them, enforced in the history/fence/fanout/reply paths."""
    try:
        await moderation_service.block_user(session, user.id, user_id)
    except moderation_service.CannotBlockSelf:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot block yourself")
    except moderation_service.UserNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")


@router.delete("/users/{user_id}/block", status_code=status.HTTP_204_NO_CONTENT)
async def unblock_user(user_id: str, user: CurrentUser, session: DbSession) -> None:
    """Remove the caller's block of ``user_id``. Idempotent (unblocking a pair
    that was never blocked is a silent no-op)."""
    await moderation_service.unblock_user(session, user.id, user_id)


@router.get("/blocks")
async def list_blocks(user: CurrentUser, session: DbSession) -> dict:
    """The users the caller has blocked (most recent first) — backs the Settings
    'Blocked users' list with display names so unblock needs no extra lookup."""
    return {"blocks": await moderation_service.list_blocks(session, user.id)}


@router.post("/messages/{message_id}/report", status_code=status.HTTP_201_CREATED)
async def report_message(
    message_id: str, req: ReportReq, user: CurrentUser, session: DbSession,
    background: BackgroundTasks,
) -> dict:
    """Report ``message_id`` as objectionable. Idempotent per (message, reporter):
    a re-report returns the existing report id. 404 if the message does not exist
    OR sits in a channel the reporter cannot READ (existence-hiding for private
    channels the caller isn't in). NOTE: the gate is channel readability, not
    per-message visibility — a soft-deleted or block-hidden message in a readable
    channel is still reportable on purpose (report-then-block / report-a-tombstone
    are valid ops flows); see get_reportable_message."""
    # Resolve the message THROUGH the channel ACL first: a reporter must be able
    # to read the channel, else the message is existence-hidden (same 404 as a
    # missing one). This prevents reporting into private channels the caller isn't
    # in — the meaningful existence boundary.
    msg = await moderation_service.get_reportable_message(session, user.id, message_id)
    if msg is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    try:
        report = await moderation_service.report_message(
            session, reporter_id=user.id, message_id=message_id, reason=req.reason.value
        )
    except moderation_service.MessageNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "message not found")
    # Best-effort operator ping — enqueued AFTER the report is committed, so a
    # webhook failure can never affect the report write, and non-blocking so it
    # can't delay the reporter's response. No-op when the URL is unset.
    if settings.moderation_alert_webhook_url:
        background.add_task(
            _deliver_moderation_alert,
            settings.moderation_alert_webhook_url,
            {
                "report_id": report.id,
                "message_id": report.message_id,
                "channel_id": msg.channel_id,
                "reason": report.reason,
                "reporter_user_id": report.reporter_user_id,
                "created_at": report.created_at.isoformat(),
                "preview": (msg.body or "")[:_ALERT_PREVIEW_MAX],
            },
        )
    return {"report_id": report.id}


# --- moderator act-on-report (Piece B) --------------------------------------
# All gated by ``ModeratorUser`` (require_moderator): a non-moderator gets 403,
# an unauthenticated caller 401, both before any row is touched. Enforcement is
# server-side; the app's /me is_moderator flag only shows/hides the UI.


@router.get("/reports")
async def list_reports(
    moderator: ModeratorUser, session: DbSession,
    status: Literal["pending"] = "pending",
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    """The moderator triage queue. Only ``status=pending`` (unresolved) is served
    today — the queue behind the EULA's 24h-action commitment. The closed set is a
    ``Literal`` so FastAPI validates it at the boundary (422 + OpenAPI-documented)
    rather than a hand-rolled check. ``limit`` is a validated page size (1..500,
    default 100) so a backlog past the default can't leave the oldest pending
    reports permanently unreachable through the API (cage-match Carnot); full
    cursor pagination is deferred to #44. Privileged read: shows already-soft-
    deleted / block-hidden context (no visibility filter)."""
    return {"reports": await moderation_service.list_pending_reports(session, limit=limit)}


@router.post("/reports/{report_id}/resolve", status_code=status.HTTP_204_NO_CONTENT)
async def resolve_report(
    report_id: str, moderator: ModeratorUser, session: DbSession, request: Request,
) -> None:
    """Act on a report by taking the reported message down (soft-delete) and
    marking the report ``taken_down``. Idempotent. 404 for an unknown report.

    Propagation (#7): the soft-delete + its forward-ULID retraction commit together
    in take_down_message; here we additionally fan the retraction to live
    subscribers so a connected client removes the message immediately. Best-effort
    over the wire — the Retraction row is the durable record, so an offline or
    single-worker-missed client self-heals on its next forward get_history catch-up.
    Single-process hub scope, identical to the ban active-disconnect (see
    ``ban_user``)."""
    try:
        retraction = await moderation_service.take_down_message(
            session, report_id=report_id, moderator_id=moderator.id)
    except moderation_service.ReportNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    except moderation_service.MessageNotFound:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "reported message no longer exists")
    except moderation_service.ReportAlreadyResolved:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "report already resolved a different way")
    except moderation_service.RetractionOrderingError:
        # Structurally unreachable (would need a clock regression); the service
        # already failed closed (session clean, report unresolved). Map it to an
        # observable 500 rather than an opaque stack trace (cage-match Tesla + Wu).
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "retraction ordering invariant violated — takedown refused")
    # None on an idempotent re-resolve, or a message already retracted by an earlier
    # report (clients already got the first retraction) — so this never double-fans.
    if retraction is not None:
        gw = getattr(request.app.state, "gw", None)
        if gw is not None and getattr(gw, "hub", None) is not None:
            # Retractions are NOT block-filtered (#7 add/remove asymmetry): a delete
            # carries no content and only reduces visibility, so it fans out to EVERY
            # subscriber — the live twin of the unfiltered history/fence retraction
            # read. No exclusion set to compute.
            #
            # Reading retraction.* here is post-commit — safe only because SessionLocal
            # runs expire_on_commit=False (same coupling create_outbound->message_view
            # and ban_user rely on). Flagged (cage-match Wu): a MissingGreenlet here
            # would 500 AFTER the durable commit and skip fanout — harmless, history
            # self-heals from the durable Retraction row.
            await gw.hub.fanout(
                retraction.channel_id,
                envelopes.retraction_frame(
                    retraction.channel_id, retraction.id, retraction.target_msg_id))


@router.post("/reports/{report_id}/dismiss", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_report(
    report_id: str, moderator: ModeratorUser, session: DbSession,
) -> None:
    """Dismiss a frivolous report (mark ``dismissed``, leave the message). Idempotent.
    404 for an unknown report."""
    try:
        await moderation_service.dismiss_report(
            session, report_id=report_id, moderator_id=moderator.id)
    except moderation_service.ReportNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "report not found")
    except moderation_service.ReportAlreadyResolved:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "report already resolved a different way")


@router.post("/users/{user_id}/ban", status_code=status.HTTP_204_NO_CONTENT)
async def ban_user(
    user_id: str, moderator: ModeratorUser, session: DbSession, request: Request,
) -> None:
    """Suspend ``user_id`` from this island. Per-island, reversible, forward-looking.
    400 for a self-ban, 404 for an unknown target. Active-disconnect (option a): after
    the ban commits, drop the banned user's live socket(s) so they can't keep posting
    on an already-open connection — the auth gates already refuse every new request,
    reconnect, and refresh."""
    try:
        await moderation_service.ban_user(
            session, target_id=user_id, moderator_id=moderator.id)
    except moderation_service.CannotBanSelf:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot ban yourself")
    except moderation_service.CannotBanModerator:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "cannot ban a moderator — remove them from the moderator set first")
    except moderation_service.UserNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
    # Active-disconnect: reach the realtime hub via app state. In production the
    # lifespan always wires app.state.gw, so this fires; it is absent only in
    # unit/route contexts without a running bus (nothing live to drop — skip
    # cleanly). SINGLE-PROCESS SCOPE (cage-match Tesla): the hub is in-process
    # memory, so this drops sockets on THIS worker only. The gateway runs a single
    # uvicorn worker today (realtime/hub.py), so that is complete; if it ever scales
    # to multiple workers/replicas, active-disconnect must fan across the fleet (the
    # same redis-fanout path the hub's multi-worker plan names) or the already-open
    # sockets on other workers ride token expiry until their next gated request.
    gw = getattr(request.app.state, "gw", None)
    if gw is not None and getattr(gw, "hub", None) is not None:
        dropped = await gw.hub.disconnect_user(user_id)
        if dropped:
            log.info("ban: dropped %s live socket(s) for user=%s", dropped, user_id)


@router.delete("/users/{user_id}/ban", status_code=status.HTTP_204_NO_CONTENT)
async def unban_user(
    user_id: str, moderator: ModeratorUser, session: DbSession,
) -> None:
    """Lift ``user_id``'s ban. Idempotent. 404 for an unknown target."""
    try:
        await moderation_service.unban_user(session, target_id=user_id)
    except moderation_service.UserNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "user not found")
