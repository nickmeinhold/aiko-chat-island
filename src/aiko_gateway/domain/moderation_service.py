"""UGC moderation — user blocks + message reports (Apple 1.2 / Google UGC, #7).

The store-rejection-blocker feature for a chat app carrying user content. Three
capabilities, one service:

  * BLOCK (mutual visibility + no interaction). A block is stored directionally
    (who pressed the button) but takes effect symmetrically: once A blocks B,
    neither sees the other's messages and neither may reply to the other. The
    enforcement is BACKEND-FIRST — a blocked user's content never loads (REST
    history), never streams (WS fanout), and a reply across a block is rejected
    at the send gate. The client may additionally hide locally, but the server is
    the boundary.

  * REPORT. A write-only record of an objectionable message. It touches no read
    path; it feeds the ops queue behind the EULA's 24h-action commitment. Acting
    on a report reuses the existing soft-delete (`Message.deleted_at`).

  * The VISIBILITY PREDICATE (`not_blocked_predicate`) — a single SQL predicate
    reused by BOTH `messages_service.get_history` AND `messages_service.latest_ulid`.
    Those two reads are coupled by design (the fence and the history pager walk
    the same visible-id axis; B4's reconnect loop asserts an empty page while
    `cursor < fence` is an invariant violation). A block is a NEW visibility
    dimension on top of `deleted_at IS NULL`; applying it to one read but not the
    other would let the fence point at a message history will never return —
    hanging the client. One predicate, both reads.

Commit convention: service-owns-commit (mirrors `accounts_service` /
`users_service`) so the REST routes stay commit-free.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import and_, delete, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from . import acl
from .ids import new_ulid
from .models import (
    Message, MessageReport, ReportResolution, Retraction, User, UserBlock)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

# The closed set of report reasons. Validated at the API boundary (pydantic enum)
# so an unknown reason is a 422, never a silently-stored free-text blob.
REPORT_REASONS = ("spam", "harassment", "hate", "violence", "sexual", "other")


class CannotBlockSelf(Exception):
    """A user tried to block their own account."""


class UserNotFound(Exception):
    """The block target / report subject does not exist."""


class MessageNotFound(Exception):
    """The reported message does not exist (or is not visible to the reporter)."""


class ReportNotFound(Exception):
    """The report id a moderator tried to act on does not exist."""


class CannotBanSelf(Exception):
    """A moderator tried to ban their own account."""


class CannotBanModerator(Exception):
    """A moderator tried to ban another configured moderator. Refused so the
    moderator set is always fully unbanned by construction — no ban can lock a
    moderator (including the last one) out of the recovery endpoint, and one mod
    can't sanction a peer. To sanction someone who is currently a moderator,
    remove them from MODERATOR_USER_IDS first, then ban (cage-match Carnot/Tesla)."""


class ReportAlreadyResolved(Exception):
    """A moderator tried to act on a report that was already resolved a DIFFERENT
    way (e.g. take down a dismissed report). Re-applying the SAME action is an
    idempotent no-op; a conflicting action is refused rather than silently
    producing a message state that disagrees with the recorded resolution."""


# --- blocks: mutations ------------------------------------------------------


async def block_user(session: AsyncSession, blocker_id: str, blocked_id: str) -> None:
    """Idempotently record that `blocker_id` blocks `blocked_id`.

    Raises `CannotBlockSelf` for a self-block and `UserNotFound` if the target
    account does not exist (a controlled 404, not an FK 500 at commit). A repeat
    block is a no-op (the composite PK already exists).

    CONCURRENCY (named MVP tradeoff, cage-match Carnot): idempotency is
    check-then-insert, not conflict-safe. Under the current single-writer SQLite
    deployment there is no race. On the public-scale Postgres path two concurrent
    identical blocks could both pass the `session.get` check and the second insert
    would raise IntegrityError (a 500) instead of the promised no-op. Accepted for
    the MVP — same precondition-rarity / recoverable-blast-radius call as the
    sole-admin TOCTOU (claude-tasks #14); the robust fix is a dialect upsert
    (`ON CONFLICT DO NOTHING`), tracked with that Postgres-migration cluster. A
    rollback-and-reread here is deliberately NOT used: rollback on the shared async
    test session raises MissingGreenlet (the trap the account-deletion PR already
    hit and rejected)."""
    if blocker_id == blocked_id:
        raise CannotBlockSelf()
    target = await session.get(User, blocked_id)
    if target is None:
        raise UserNotFound()
    already = await session.get(UserBlock, (blocker_id, blocked_id))
    if already is not None:
        return  # idempotent
    session.add(UserBlock(blocker_user_id=blocker_id, blocked_user_id=blocked_id))
    await session.commit()


async def unblock_user(session: AsyncSession, blocker_id: str, blocked_id: str) -> None:
    """Remove `blocker_id`'s block of `blocked_id`. Idempotent — unblocking a
    pair that was never blocked is a silent no-op (DELETE affects 0 rows)."""
    await session.execute(
        delete(UserBlock).where(
            UserBlock.blocker_user_id == blocker_id,
            UserBlock.blocked_user_id == blocked_id,
        )
    )
    await session.commit()


async def list_blocks(session: AsyncSession, blocker_id: str) -> list[dict]:
    """The users `blocker_id` has blocked (most recent first), with display name
    so the client can render an unblockable list without a second round-trip."""
    rows = (await session.execute(
        select(UserBlock, User)
        .join(User, User.id == UserBlock.blocked_user_id)
        .where(UserBlock.blocker_user_id == blocker_id)
        .order_by(UserBlock.created_at.desc())
    )).all()
    return [
        {
            "user_id": u.id,
            "display_name": u.display_name,
            "created_at": b.created_at.isoformat(),
        }
        for b, u in rows
    ]


# --- blocks: enforcement helpers --------------------------------------------


def not_blocked_predicate(viewer_id: str):
    """SQL predicate: this `Message` is NOT hidden from `viewer_id` by a block.

    Mutual: a message authored by S is hidden from viewer V if a block exists in
    EITHER direction between V and S. A correlated NOT EXISTS so it composes into
    a single statement (no per-row round-trip). Messages with a NULL
    `sender_user_id` (external bus actors — LLM/robot/REPL) are ALWAYS visible:
    NULL never equals a user id, so the EXISTS is empty and the NOT EXISTS holds.
    Used identically by `get_history` and `latest_ulid` (the fence) so the
    visible-id axis they share stays consistent."""
    blocked = exists().where(
        or_(
            and_(
                UserBlock.blocker_user_id == viewer_id,
                UserBlock.blocked_user_id == Message.sender_user_id,
            ),
            and_(
                UserBlock.blocked_user_id == viewer_id,
                UserBlock.blocker_user_id == Message.sender_user_id,
            ),
        )
    )
    return ~blocked


async def blocked_pair_user_ids(session: AsyncSession, user_id: str) -> set[str]:
    """Every user in a block relationship (either direction) with `user_id`.

    The fanout EXCLUSION set: when `user_id` sends a message, no connection owned
    by one of these users may receive it (they blocked the sender, or the sender
    blocked them — mutual). Computed once per send, then applied in-memory in the
    hub, so fanout costs one indexed query rather than one per connection."""
    rows = (await session.execute(
        select(UserBlock.blocker_user_id, UserBlock.blocked_user_id).where(
            or_(
                UserBlock.blocker_user_id == user_id,
                UserBlock.blocked_user_id == user_id,
            )
        )
    )).all()
    out: set[str] = set()
    for blocker, blocked in rows:
        out.add(blocked if blocker == user_id else blocker)
    return out


async def is_blocked_between(session: AsyncSession, a_id: str, b_id: str) -> bool:
    """Whether a block exists in EITHER direction between two users. The
    interaction gate (e.g. a reply across a block) consults this — the single
    door any future interaction surface (DMs, mentions) must also pass through."""
    if a_id == b_id:
        return False
    return bool((await session.execute(
        select(
            exists().where(
                or_(
                    and_(UserBlock.blocker_user_id == a_id,
                         UserBlock.blocked_user_id == b_id),
                    and_(UserBlock.blocker_user_id == b_id,
                         UserBlock.blocked_user_id == a_id),
                )
            )
        )
    )).scalar())


# --- reports ----------------------------------------------------------------


async def get_reportable_message(
    session: AsyncSession, viewer_id: str, message_id: str
) -> Message | None:
    """The message iff `viewer_id` may report it, else None. The gate is CHANNEL
    readability (public, or private-where-member): a message in a channel the
    reporter cannot see is existence-hidden behind the same None as a missing
    message (the route maps both to 404). Resolves message → channel → ACL.

    NOT gated on per-message visibility (soft-delete / block), and deliberately so
    (cage-match Carnot MEDIUM): reporting is for ops, and the legitimate flows
    "report a user then block them" and "report a message that was just deleted"
    both need to reach a message the *current* read path would hide. The route
    returns only an opaque report id, never message content, so this leaks nothing
    beyond what channel-ACL already governs."""
    msg = await session.get(Message, message_id)
    if msg is None:
        return None
    channel = await acl.readable_channel(session, viewer_id, msg.channel_id)
    if channel is None:
        return None
    return msg


async def report_message(
    session: AsyncSession, *, reporter_id: str, message_id: str, reason: str,
) -> MessageReport:
    """Record a report of `message_id` by `reporter_id`. Idempotent on
    (message, reporter): a re-report returns the existing row rather than
    stacking duplicates. Raises `MessageNotFound` if the message does not exist.

    Visibility of the reported message is the CALLER's responsibility (the route
    resolves it through the channel ACL first) — this service only guards
    existence so a bogus id is a controlled 404, not an FK 500 at commit."""
    msg = await session.get(Message, message_id)
    if msg is None:
        raise MessageNotFound()
    existing = (await session.execute(
        select(MessageReport).where(
            MessageReport.message_id == message_id,
            MessageReport.reporter_user_id == reporter_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        return existing  # idempotent
    row = MessageReport(
        id=new_ulid(),
        message_id=message_id,
        reporter_user_id=reporter_id,
        reason=reason,
    )
    session.add(row)
    await session.commit()
    return row


# --- moderator act-on-report (Piece B) --------------------------------------


def is_moderator(user_id: str) -> bool:
    """Whether `user_id` is a configured site moderator. Config lookup, no DB —
    backs the /me `is_moderator` flag and mirrors the `require_moderator` gate, so
    the flag the client sees and the gate the server enforces read one source."""
    return user_id in settings.moderator_user_ids


async def list_pending_reports(session: AsyncSession, *, limit: int = 100) -> list[dict]:
    """The moderator queue: unresolved reports (`resolved_at IS NULL`), newest
    first, joined to the reported message (body preview + channel) and the reporter
    (display name; OUTER join because a report's reporter may be anonymized to NULL
    by account deletion).

    PRIVILEGED read — applies NO visibility filter (`deleted_at` / block predicate)
    so a moderator sees already-soft-deleted or block-hidden context. The route
    gates it hard behind `require_moderator`."""
    rows = (await session.execute(
        select(MessageReport, Message, User)
        .join(Message, Message.id == MessageReport.message_id)
        .outerjoin(User, User.id == MessageReport.reporter_user_id)
        .where(MessageReport.resolved_at.is_(None))
        .order_by(MessageReport.created_at.desc())
        .limit(limit)
    )).all()
    return [
        {
            "report_id": r.id,
            "message_id": r.message_id,
            "channel_id": m.channel_id,
            "reason": r.reason,
            "reporter_user_id": r.reporter_user_id,
            "reporter_display_name": u.display_name if u is not None else None,
            "created_at": r.created_at.isoformat(),
            "message_body": m.body,
            "message_sender_user_id": m.sender_user_id,
            "message_deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
        }
        for r, m, u in rows
    ]


async def take_down_message(
    session: AsyncSession, *, report_id: str, moderator_id: str,
) -> Retraction | None:
    """Act on a report by taking the message down: soft-delete the message (first
    writer of `Message.deleted_at`) and resolve the report as `taken_down`.

    A report resolves EXACTLY ONCE, to one outcome. Re-running take-down on an
    already-taken-down report is an idempotent no-op; running it on an already-
    DISMISSED report raises `ReportAlreadyResolved` rather than soft-deleting the
    message while the recorded resolution still reads `dismissed` (a state/label
    disagreement — cage-match Carnot HIGH + Tesla). The resolution gate and the
    soft-delete are therefore NOT independent: the message is only ever deleted on
    the transition that also stamps `taken_down`. Raises `ReportNotFound` for a bad
    id. The soft-delete propagates to the read paths through the existing
    `deleted_at IS NULL` filter shared by `get_history` / `latest_ulid`, so the
    message vanishes from history and the fence at once.

    PROPAGATION (#7): the soft-delete alone only hides the message from FUTURE
    reads — a client that already synced it (id <= its forward watermark) never
    re-observes the deletion. So the takedown TRANSITION also appends a forward-ULID
    `Retraction` in the SAME transaction (see the Retraction model docstring): a new
    row with its own higher ULID that rides history catch-up + WS fanout to reach
    both offline and live clients. Returns the created `Retraction` so the caller
    can fan it out live; returns None on the idempotent re-resolve (clients already
    received the first retraction). Emitted even when the message was already
    soft-deleted (e.g. a prior account-deletion husk) — a client may still hold the
    pre-takedown row and needs the removal signal.

    CONCURRENCY (named MVP tradeoff, mirrors `block_user`): the resolved-check is
    read-then-write, not an atomic conditional UPDATE. Under the single-writer
    SQLite deployment there is no race. On the future Postgres path two concurrent
    moderators could both pass the `resolved_at is None` check; the robust fix is a
    guarded `UPDATE ... WHERE resolved_at IS NULL` + rowcount, tracked with that
    migration cluster."""
    report = await session.get(MessageReport, report_id)
    if report is None:
        raise ReportNotFound()
    if report.resolved_at is not None:
        if report.resolution == ReportResolution.TAKEN_DOWN:
            return None  # idempotent: same action, already applied (already emitted)
        raise ReportAlreadyResolved()
    now = _utcnow()
    message = await session.get(Message, report.message_id)
    if message is not None and message.deleted_at is None:
        message.deleted_at = now
    report.resolved_at = now
    report.resolution = ReportResolution.TAKEN_DOWN
    report.resolved_by_user_id = moderator_id
    # Forward-ULID retraction, atomic with the soft-delete. Scoped to the message's
    # channel so get_history pages it on the same (channel_id, id) axis. `id` defaults
    # to a fresh new_ulid() (time-forward), strictly greater than the target's id, so
    # the forward cursor always carries it. Skipped only if the target row is somehow
    # missing (no channel to scope to) — reports FK a non-null message_id and messages
    # are never hard-deleted, so in practice a retraction is always emitted here.
    retraction: Retraction | None = None
    if message is not None:
        retraction = Retraction(
            target_msg_id=message.id, channel_id=message.channel_id)
        session.add(retraction)
    await session.commit()
    return retraction


async def dismiss_report(
    session: AsyncSession, *, report_id: str, moderator_id: str,
) -> None:
    """Dismiss a frivolous report: stamp resolution `dismissed`, leave the message
    untouched. Re-dismissing is an idempotent no-op; dismissing an already-TAKEN-
    DOWN report raises `ReportAlreadyResolved` (symmetric with `take_down_message`
    — one resolution per report, conflicting re-resolution refused). Raises
    `ReportNotFound` for a bad id."""
    report = await session.get(MessageReport, report_id)
    if report is None:
        raise ReportNotFound()
    if report.resolved_at is not None:
        if report.resolution == ReportResolution.DISMISSED:
            return  # idempotent: same action, already applied
        raise ReportAlreadyResolved()
    report.resolved_at = _utcnow()
    report.resolution = ReportResolution.DISMISSED
    report.resolved_by_user_id = moderator_id
    await session.commit()


async def ban_user(session: AsyncSession, *, target_id: str, moderator_id: str) -> User:
    """Suspend `target_id` from this island (set `banned_at`). Per-island,
    reversible (see `unban_user`), forward-looking — it does not delete the row or
    the user's past messages. Idempotent: a re-ban keeps the first `banned_at`.
    Raises `CannotBanSelf` (a moderator can't lock themselves out) and
    `UserNotFound` for a missing target. Returns the target row so the caller can
    drop the banned user's live socket(s) (active-disconnect enforcement)."""
    if target_id == moderator_id:
        raise CannotBanSelf()
    # A configured moderator can't be banned (demote via MODERATOR_USER_IDS first).
    # Guarantees the moderator set is always fully unbanned -> no ban can lock the
    # last moderator out of the recovery endpoint, and no mod can sanction a peer.
    if is_moderator(target_id):
        raise CannotBanModerator()
    target = await session.get(User, target_id)
    if target is None:
        raise UserNotFound()
    if target.banned_at is None:
        target.banned_at = _utcnow()
    await session.commit()
    return target


async def unban_user(session: AsyncSession, *, target_id: str) -> None:
    """Lift `target_id`'s ban (clear `banned_at`). Idempotent (unbanning an active
    user is a no-op). Raises `UserNotFound` for a missing target."""
    target = await session.get(User, target_id)
    if target is None:
        raise UserNotFound()
    target.banned_at = None
    await session.commit()


# --- account-deletion cascade (called by accounts_service) ------------------


async def purge_user_moderation_rows(session: AsyncSession, user_id: str) -> None:
    """Tear down a deleting user's moderation footprint, WITHOUT committing (the
    caller's account-deletion transaction owns the commit). Mirrors how
    `accounts_service` handles every other child of `users`:

      * blocks (either direction) are DELETED — a block by or of a now-gone
        account is meaningless.
      * reports authored by the user are ANONYMIZED (reporter_user_id → NULL),
        not deleted — the report still drives ops action; only the PII link goes,
        exactly as authored messages are anonymized rather than shredded.
      * reports this user RESOLVED as a moderator are likewise anonymized
        (resolved_by_user_id → NULL) — the resolution + audit trail stay, only the
        deleted moderator's identity link is severed (Piece B).
    """
    await session.execute(
        delete(UserBlock).where(
            or_(
                UserBlock.blocker_user_id == user_id,
                UserBlock.blocked_user_id == user_id,
            )
        )
    )
    await session.execute(
        update(MessageReport)
        .where(MessageReport.reporter_user_id == user_id)
        .values(reporter_user_id=None)
    )
    await session.execute(
        update(MessageReport)
        .where(MessageReport.resolved_by_user_id == user_id)
        .values(resolved_by_user_id=None)
    )
