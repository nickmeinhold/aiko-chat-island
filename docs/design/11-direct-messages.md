# Design 11 — Direct messages as 1:1 (member-set) channels (#2633)

**Status:** built, awaiting cage-match. **Contract of record:**
`HANDOFF-to-app-tab-v2-social-wire.md` §#2633 + the app-tab replies on tracker
`nickmeinhold/claude-tasks#2633` (2026-08-06). This note records the *gateway-side*
decisions that are NOT in the ticket — the ones a reviewer needs the reasoning for.

## What a DM is

A DM is an ordinary `Channel` with `kind="dm"`, `is_private=True`,
`community_id=NULL`, and a `Membership` row per participant. **No new table, no new
column, no migration.** The schema already permits this exact shape:
`ck_channels_community_required` (`kind='dm' OR community_id IS NOT NULL`) exempts a DM
from the community requirement, and `Membership` is already an N-capable join table.

Reusing the channel/message machinery means DMs inherit — for free and through the
*same* enforcement door — auth (I1), membership visibility (I2, `acl.readable_channel`),
existence-hiding (404-not-403), signed `origin` carriage, mentions, reply-to integrity,
takedown retractions, reactions, and the block content-filter.

## Decision 1 — DMs are member-SET channels, not 2-capped (app-tab shaping request)

The app tab asked (tracker #2633, `from-app-tab: dm-memberset-not-2capped`) that we not
foreclose group chats. Groups are "the same object with N members". So:

- **No `member_a`/`member_b` columns, no `CHECK(count=2)`.** Membership stays the N-able
  relation it already is. `kind="group"` becomes a later *additive* kind, not a retype.
- **`members` is an array on the wire** (`["<keyA>","<keyB>"]`), never a `peer` scalar.
- **The authz predicate is "requester ∈ members"** — expressed as set-membership
  (`acl.readable_channel` / `acl.can_post`, which test for a `Membership` row), so it is
  N-safe verbatim when groups arrive.
- **2-ness lives ONLY at `POST /v1/dm`** — the find-or-create endpoint, which is
  inherently pairwise. Groups get their own `POST /v1/groups` later; `/v1/dm` is untouched.

## Decision 2 — the canonical pair IS the idempotency key (no `dm_pairs` table)

`POST /v1/dm` must be idempotent: the unordered pair `{me, target}` always resolves to
the same channel. Rather than a side `dm_pairs(lo, hi) UNIQUE` table, we mint a
**deterministic `aiko_channel`** from the canonically-sorted member ids:

```
lo, hi = sorted([me_id, target_id])          # ULIDs sort lexicographically
aiko_channel = f"dm:{lo}:{hi}"               # 3 + 26 + 1 + 26 = 56 ≤ 64 (String(64))
```

`channels.aiko_channel` is already `UNIQUE NOT NULL`, so find-or-create is **atomic on
the existing constraint**: INSERT the channel+memberships in one transaction; on
`IntegrityError` (a concurrent double-tap won the race) roll back and re-fetch by
`aiko_channel`. This is the "remove the coupling, don't guard the window" move — there
is no separate table to keep in sync, and the `dm:` prefix confines 2-ness to the DM
endpoint (Decision 1 stays intact; a group would mint a ULID `aiko_channel`, not a pair).

A self-DM (`target == me`, allowed — see Decision 4) collapses to `dm:{me}:{me}` with a
single membership row and `members == [me]` — the notes-to-self channel.

## Decision 3 — DMs do NOT federate on the aiko bus (the privacy gate)

`realtime/ws.py._handle_send` publishes **every** created message to the shared
ChatServer bus (`gw.bus.send(user.aiko_username, channel.aiko_channel, body)`). A DM
routed through that path would broadcast private content onto the federation backbone —
a serious leak. So the bus publish (and its echo-suppression bookkeeping) is now **gated
on `channel.kind != "dm"`**. A DM message is still persisted, ack'd, and fanned out to
the channel's *local* WS subscribers (both members), but **nothing crosses the bus**.

This makes DMs **island-local by construction**: both participants must be on the same
island. Cross-island DMs are the separate *sealed-sender / signed-not-sealed* federation
track (app-tab #1962), explicitly out of scope here. The bus-ingest path
(`persist_inbound`) can never mint a `dm:` channel either — DM `aiko_channel`s are never
in the `channel_list` EC share and never subscribed, so the reconcile worker
(`upsert_channel` / `hard_delete_channel`) never names them.

## Decision 4 — settled open items (from the app tab, 2026-08-06)

- **Self-DM → ALLOW** (notes-to-self, Telegram "Saved Messages"). `POST /v1/dm` with
  `target == me` returns the 1-member channel, never 400.
- **Unread → CLIENT-SIDE.** No `read_positions` store on the island for v2. `GET /v1/dm`
  does **not** emit a fabricated `unread`; the app drives it off its own last-seen
  watermark (already shipped app-side).
- **`GET /v1/messages/{id}`** (reply-parent resolution) applies the *same* visibility
  predicate as history (`messages_service.visible_message`): a missing / soft-deleted /
  unreadable / blocked-author message all 404 identically (existence-hiding). It does
  NOT resurrect a taken-down parent's body. A dedicated retracted-tombstone shape (so a
  quote can render "message removed" rather than "unavailable") is an additive follow-up
  the app tab can request — noted, not built, to avoid inventing wire shape unilaterally.

## Decision 5 — block is a CONTENT filter, not a creation gate (flagged fork)

`POST /v1/dm` does **not** refuse a DM between users in a block relationship, and does
not consult `is_blocked_between`. Rationale: the shared read/fanout path already
neutralizes a DM under a block — `get_history` and the live-fanout `exclude` both drop
blocked-pair messages in both directions — so the channel is **inert** (no content flows
either way) without a creation gate. This matches how @-mentions moved block enforcement
off carriage and onto the interaction/notification layer (#2632). A creation-time gate
would also leak the block *direction* (returning "blocked" tells A that B blocked them),
which existence-hiding forbids.

**This is a fork worth a second opinion** (moderation semantics). The alternative —
fold a blocked target into the same 404 as a missing user at creation time — is a
one-line addition if the app tab / Nick prefers "you cannot open a DM with someone
you've blocked". Default shipped: no creation gate, block stays a content filter.

## Exclusion from `GET /v1/channels`

A DM is `is_private=True`, so it would otherwise appear in `acl.visible_channels` (the
flat list backing `GET /v1/channels`). Per the contract, DMs are **excluded from that
list** — added as a `kind != "dm"` clause in `visible_channels` ONLY. The readable
predicate (`readable_channel`, `filter_readable_ids`) is untouched, so a member can
still *subscribe* to and read their DM over WS; the client learns its DM channel ids
from `GET /v1/dm`, not the flat list.

## Endpoints

```
POST /v1/dm   { "target_user_id": "<key>" }   → 200 {channel_id, kind, members[], created_at}
                                                 404 if target isn't a real user
GET  /v1/dm                                    → 200 {channels: [{...channel, last_message}]}
GET  /v1/messages/{msg_id}                     → 200 MessageView | 404 (existence-hiding)
```

`last_message` is the newest message VISIBLE to the caller (not soft-deleted, author not
blocked) or `null`. No `unread` (Decision 4).
</content>
