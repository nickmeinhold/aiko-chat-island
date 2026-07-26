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
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel

from ..config import settings
from ..domain import moderation_service
from .deps import CurrentUser, DbSession

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
