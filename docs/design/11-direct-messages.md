# Design 11 — Direct messages as 1:1 (member-set) channels (#2633)

**Status:** built, awaiting cage-match. **Contract of record:**
`HANDOFF-to-app-tab-v2-social-wire.md` §#2633 + the app-tab replies on tracker
`nickmeinhold/claude-tasks#2633` (2026-08-06). This note records the *gateway-side*
decisions that are NOT in the ticket — the ones a reviewer needs the reasoning for.

## What a DM is

A DM is an ordinary `Channel` with `kind="dm"`, `is_private=True`,
`community_id=NULL`, `join_policy="invite_only"`, and a `Membership` row per participant.
**No new table and no new column** — DMs reuse the existing `Channel`/`Membership` shape.
**Two additive migrations** (cage-match hardening): **0020** adds/tightens the DM CHECK
constraints on `channels` — `ck_channels_kind` (closed `ChannelKind` set), bidirectional
`ck_channels_community_required` (DM ⟺ NULL community), `ck_channels_dm_private` (DM ⇒
private), `ck_channels_dm_prefix` (DM ⟺ `dm:` prefix, case-sensitive), and
`ck_channels_dm_invite_only`; **0021** adds a `memberships.user_id` index. `Membership` is
already an N-capable join table.

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

## Decision 5 — a DM SEND under a block is REFUSED (Nick's ruling 2026-08-10)

`POST /v1/dm` still does **not** block-gate creation (a creation-time refusal would leak
the block *direction*, and the channel shell is harmless). But the **send** is gated: in
`_handle_send`, if the channel is a DM (`kind == "dm"`) and the sender is in a block
relationship with the DM peer, the send is **refused**, collapsed into the SAME
existence-hiding `no_channel` error as a missing/unreadable channel (so the refusal never
reveals who blocked whom, and is symmetric in both block directions).

Why refuse rather than filter (the cage-match fork, PR#124 Tesla): a public channel keeps
block as a read-only *content filter* (a blocked author's message is still seen by
everyone else). But a **1:1 room** has no "everyone else" — a blocked sender's message
reaches nobody, so a persisted-but-filtered row is pure dead storage that an **unblock
would resurface** ("inbox of ghosts"). Refusing the send means no residue accrues.
DM-specific (keyed on `kind == "dm"`) — public/community block semantics are unchanged.

Enforced at the **mutator door** (`messages_service.create_outbound`, which raises
`BlockedDmSend`), not the WS route — so *every* send path (the WS route today, any future
REST/bot/in-process writer) honours it (repo law: seal the shared writer, not each caller;
PR#124 Tesla P1a). The WS route maps `BlockedDmSend` to `no_channel`.

**Named tradeoff (PR#124 Tesla P1b):** the refusal reuses the existence-hiding
`no_channel` code, but for a DM the *channel* existence is not actually hidden — the
client just created/listed it, so it holds the id. `no_channel` here is chosen for
symmetry (identical in both block directions — no distinct "blocked" code) and to avoid a
new error variant, *not* to hide the channel. A blocked-by party who holds the DM id can
infer a block exists (as on any platform), but not, from the code alone, its direction.
We accept that over adding a directional `blocked` code.

**Write-once invariant (PR#124 Tesla P3):** the bus-federation suppression rests on BOTH
`kind == "dm"` AND the `dm:` `aiko_channel` prefix (the dual gate). Both are set once at
DM creation and never mutated — `kind` is closed by `ck_channels_kind`, and the `dm:`
prefix ⟺ `kind='dm'` **bidirectionally and case-sensitively** at the DB
(`ck_channels_dm_prefix`), so the `dm:` namespace is totally reserved (no writer can squat
it) and a `kind` retint can't strip the prefix leg. A future mutator must treat both as
immutable for a DM row.

**Scope of "no residue" (PR#124 Tesla P2 — read precisely):** Decision 5's "no residue"
means no **message rows** accrue under a block (the send is refused). It does NOT mean the
DM **channel object** disappears: `GET /v1/dm` lists a DM by membership ∩ `kind='dm'` with
no block filter, so a DM shell (with a `null` `last_message` under a block) stays in both
parties' switchers. That is consistent with unread being client-side — **the client hides
a conversation**; the island keeps the channel. Blocking freezes new content at the
switcher too (no new `last_message`), but does not tombstone the shell.

**Known gap — DM consent semantics (PR#124 Tesla P3, app-tab-owned):** `POST /v1/dm` is
consentless find-or-create + immutable membership — anyone who knows your `user_id` mints a
permanent co-membership edge; block stops speech, not presence; there is no
accept/request/dismiss state server-side. Island-local privacy is solid; **social**
consent is a v2 product decision the app tab owns (client-side hide is the current answer).
Tracked as a follow-up so the group PR doesn't rediscover it — see the tracker.

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
blocked) or `null`. No `unread` (Decision 4). `GET /v1/messages/{id}` lives in the
**messages** router (a general messages surface, not DM-specific).

## Cage-match hardening (PR#124, Carnot + Tesla)

The adversarial review surfaced that the privacy invariant, as first built, rested on
two unenforced assumptions. Both are now enforced, not merely documented:

1. **`dm:` namespace collision — fail closed.** `aiko_channel` is shared with
   bus-reconciled channels, so a non-DM channel could (however improbably — the key
   embeds two unguessable ULIDs) hold a `dm:<lo>:<hi>` key. `get_or_create_dm` now
   **raises `DmKeyCollision` (→ 409)** rather than adopting a non-DM channel as a DM
   (which would federate its sends and graft membership onto an unrelated channel).

2. **`dm:` is reserved at the bus-reconcile boundary.** `channels_service.upsert_channel`
   raises `ReservedDmChannel` and `hard_delete_channel` no-ops for any `dm:`-prefixed
   name, and `persist_inbound` drops a bus message named for one. So the bus can never
   mint, route into, or hard-delete a private DM — the invariant no longer depends on
   the bus operator never naming a `dm:` channel. The prefix is a single source of truth
   (`dm_service.DM_CHANNEL_PREFIX` / `is_dm_channel_name`).

3. **`kind` is a closed set at the DB.** The privacy gate keys on `channel.kind == "dm"`,
   so `kind` is now a `ChannelKind` StrEnum with a `ck_channels_kind` DB CHECK (migration
   0020) — the same closed-set-at-the-DB posture Role/JoinPolicy/Visibility already have.
   An out-of-set kind is unrepresentable, so a bad writer can't mint a kind that bypasses
   the gate. (Verified safe against live prod: every existing channel is `standard`.)

4. **`_ensure_memberships` is race-safe.** Each missing membership insert is wrapped in a
   SAVEPOINT so a concurrent repair converges (idempotent) instead of 500-ing on the
   composite PK — correcting an earlier comment that wrongly claimed a plain ORM re-insert
   is a silent no-op.

5. **`GET /v1/dm` is batched** — members + last-message resolve in a constant number of
   queries (`members_of_many` + `last_visible_messages`), not per-channel, so the switcher
   endpoint has no N+1.

### Decision 5 resolved (Nick, 2026-08-10): refuse the send

Tesla's sharpest non-mechanical point — block-as-content-filter leaves a DM
*readable-inert* but not *storage-inert*, so an unblock resurfaces the backlog — was put
to Nick as a product fork. Ruling: **refuse the DM send under a block** (see the rewritten
Decision 5 above). Implemented in `_handle_send`, tested in
`test_dm_send_under_block_is_refused` (symmetric, existence-hiding). Public-channel block
semantics are unchanged.
</content>
