# Handoff → app tab: v2 social-layer wire contracts — 2026-08-06

The island half of the v2 social push. Four features you're building consumers for
today (`aiko_chat_app` v2), tracked island-side as **#2631–2634**. This doc is the
**wire contract you can build against NOW** — the shapes are published here before the
island implements them, because a `/v1` contract is **append-only** (see below), so
anything you build to these shapes keeps working as the island fills them in.

Build order island-side: **#2631 → #2634 → #2632 → #2633** (smallest/least-risk first;
DMs last). Each ships as its own PR + cross-family cage-match. This doc updates in place
as each lands; every section is tagged **[shape final]** or **[open]**.

## Design decisions locked 2026-08-06 (read before building)

Forward-proofing calls made now, because a `/v1` field's *meaning* can't change once
you ship against it (append-only, below). These are cheap today, a `/v2` if skipped.

1. **`body` is Markdown.** Every message `body` is Markdown text. Render it as such
   from day one — deciding this later would silently reinterpret every message already
   on the wire. (Attachments/media are a *separate additive* field later; they ride
   out-of-band as URLs, never bytes in `body`.)
2. **Message identity is `msg_id`; support in-place replace + removal.** Key every
   message by `msg_id`, never by array index or content. Edits (future), retractions
   (shipped), and reaction deltas (#2634) all arrive as "apply this to `msg_id`". A
   client that keys on anything else breaks on the first mutation.
3. **No threads; `reply_to` is the reply primitive — iMessage-style chaining, flat.**
   We are NOT building Slack-style threads (a separately-unread, separately-subscribed
   side channel per root). A channel stays one totally-ordered stream; `reply_to` links
   a message to its parent *in that stream*. Rationale: threads would fork the island's
   entire fence/watermark/catch-up/federation sync spine per-thread — the system's
   hardest axis — to solve a big-busy-channel problem an island doesn't have yet.
   - **The model is iMessage's, exactly:** a single `reply_to` parent pointer; a
     "chain" is the connected component over those edges (A ← B ← C, or many replies to
     one parent). No `thread_id`, no extra field — the chain is reconstructable from
     the one pointer. You MAY render a client-side focused "thread view" (dim the rest,
     show the chain) — reading UX with no server object.
   - What that does NOT give you: per-thread unread / per-thread subscribe. Those need
     a server-side thread object we're deliberately not building. If density ever
     forces real threads, `thread_id` is backfillable from `reply_to` chains — so
     nothing here is a dead end, and no field is reserved for it now.
   - **Parent-preview resolution (the one real decision iMessage forces):** to draw the
     quoted-parent bubble on a reply, you need the parent's sender+snippet — but the
     parent may be scrolled out of your loaded window, or arrive live while it's beyond
     your fetched history. Contract answer: **resolve the parent by id** via
     `GET /v1/messages/{id}` (below), NOT a denormalized snapshot on the reply. Reason:
     a denormalized snippet of a later **taken-down** parent would resurrect deleted
     content in every quote — a retraction leak. Resolve-by-id always returns the
     parent's *current* (incl. retracted) state, so revocation is honored on every
     render. You already have `reply_to` (the id); resolve locally when the parent is
     loaded (the common, free case), fetch by id when it isn't. (If your offline/scroll
     UX genuinely needs an instant denormalized preview, say so — we'd design a
     retraction-safe variant rather than a raw snapshot.)
4. **Unread/receipts wait on read cursors.** There's no server-side read-position store
   yet. Unread badges, "seen by", delivery/read receipts all depend on one primitive
   (a per-user per-channel read cursor), designed alongside DMs (#2633). Until it
   lands, drive unread client-side off your own last-seen.

## Read this first: snake_case, not camelCase

The task descriptions (#2631–2634) were written in camelCase (`userId`, `displayName`,
`reactedByMe`, `retryAfter`) — that's Dart/TS convention leaking into the tracker text.
**The real wire is snake_case, everywhere**, matching every existing `/v1` frame
(`msg_id`, `channel_id`, `client_msg_id`, `aiko_username`, `target_msg_id`,
`channel_fences`). Field names below are the ones the island actually emits/accepts.
Map to your Dart models on your side; do not expect camelCase off the wire.

## Why you can build before the island implements: `/v1` is append-only

Wire shapes don't "freeze" by fiat — they freeze because two independently-deployed
programs (this gateway + your app on phones behind store review) come to depend on the
same bytes with no shared deploy cadence. So within `/v1`:

- **Additive is safe.** A new field/frame-type an old client never heard of is ignored.
  Keep degrading unknown WS frame types safely (you already do — retraction handoff).
- **Rename/remove/retype is a break.** Never silently changed. If a shape below is
  wrong, it changes here *before* you've shipped against it, or it waits for `/v2`.

Everything marked **[shape final]** is safe to code to now.

---

## #2631 — `PATCH /v1/me` (handle + display-name mutate path) **[shape final]**

The mutate path that unblocks app "change handle" + "edit display name" (#2513). Today
the island only has `claimHandle` (initial provisioning) — no mutate path existed.

**Request** — authenticated (Bearer). Both fields optional; **at least one required**:

```json
PATCH /v1/me
{ "handle": "new_handle", "display_name": "New Name" }
```

- `handle` — same rules as claim: 1–64 chars, `[A-Za-z0-9_]` (other chars are
  sanitized into the wire `aiko_username`), non-blank after strip. **Unique** at a
  time. **Cooldown: 30 days** between handle *changes* (confirmed with Nick).
  - Setting `handle` to your *current* handle is a no-op — it does NOT consume or trip
    the cooldown.
- `display_name` — editable anytime, non-unique, ≤128 chars. Never subject to cooldown.

**Response 200** — the updated profile (identical shape to `GET /v1/me`):

```json
{ "user_id": "...", "username": "new_handle", "display_name": "New Name", "aiko_username": "new_handle" }
```

> Note the response field is `username` (matches `GET /v1/me`), while the request field
> is `handle`. Both words already exist in the codebase (register uses `username`,
> social-claim uses `handle`); they denote the same thing. Send `handle`, read
> `username`.

**Errors**

| Status | When | Body |
|--------|------|------|
| `400` | neither field present, or invalid handle shape | `{"detail": "..."}` |
| `409` | `handle` is taken by another user | `{"detail": "handle already taken"}` |
| `429` | handle changed within the 30-day window | `{"detail": "handle change on cooldown", "retry_after": <seconds>}` + `Retry-After` header |
| `401` | unauthenticated | — |

`retry_after` is whole seconds until the cooldown lifts. Surface it as "you can change
your handle again in N days".

---

## `GET /v1/messages/{id}` — fetch one message by id **[proposed]**

New endpoint supporting reply-parent resolution (above), deep-links, notification
taps, and jump-to-message. Authenticated.

```
GET /v1/messages/{msg_id}   → 200 MessageView  |  404 if not visible to you
```

- Same authz + visibility as history: `404` (not `403`) if the message's channel
  isn't visible to you (existence-hiding). A **retracted/taken-down** parent returns
  its retracted state (so a reply preview honors the takedown) rather than 404 — [open]
  confirm you want the retracted shape here vs a 404; leaning retracted-state so the
  quote can render "message removed".
- Returns the same `MessageView` as history/WS (single serializer), so no new shape.

## #2634 — emoji reactions wire **[shape final + signing decided]**

Island half only — the composer emoji *picker* is app-side. This is the reactions wire.

**Endpoints** — authenticated:

```
POST   /v1/messages/{msg_id}/reactions   { "emoji": "👍" }     → 200, add my reaction
DELETE /v1/messages/{msg_id}/reactions/{emoji}                 → 200, remove it (toggle)
```

- Idempotent: one reaction per (user, message, emoji). Re-POST is a no-op 200;
  DELETE of one you don't have is a no-op 200.
- Authz: you must be able to see the message's channel (member of a private channel /
  DM; any authed user for a public channel). `404` if the message isn't visible to you
  (existence-hiding — same posture as the rest of the API).

**Delivery — with the message, aggregated + viewer-dependent.** `MessageView` gains a
`reactions` array, **omitted when empty** (same convention as `origin`):

```json
"reactions": [
  { "emoji": "👍", "count": 3, "reacted_by_me": true },
  { "emoji": "🎉", "count": 1, "reacted_by_me": false }
]
```

`reacted_by_me` is computed per-viewer at read time (like the block predicate) — the
same message serialized to two users carries different `reacted_by_me`. `reactors` (the
list of keys) is **not** in the aggregate by default (privacy + payload) — [open]
whether a `?reactors=1` expansion is added; assume absent for now.

**Real-time** — a new additive WS frame (server→client), alongside
`ack`/`message`/`suback`/`retraction`/`error`:

```json
{ "type": "reaction", "channel_id": "<cid>", "msg_id": "<mid>", "emoji": "👍", "user_id": "<reactor_key>", "action": "add", "origin": { ... } }
```

`action` is `"add"` or `"remove"`. Apply it as a delta to that message's aggregate;
compute your own `reacted_by_me` by comparing `user_id` to yourself. This mirrors the
discrete `retraction` frame rather than re-broadcasting the whole message (lighter, and
it's the established pattern). Unknown-frame safe-degrade means you can ship optimistic
toggle before consuming the frame.

### Reactions are SIGNED from day one (decided 2026-08-06, Nick + app tab)

A reaction is a **signed lightweight endorsement**, not throwaway UI sugar — the raw
material the Carried Record (#2506) judgment-half can later ingest. Because you can't
sign history retroactively, the signature is captured **from the first #2634 reaction**,
not bolted on later. This is a wire/model decision; the UI can stay count-only.

- **Envelope:** the reaction carries the SAME `origin` shape as a signed message
  (#1816) — `{v, alg, key_version, sender_pubkey, client_msg_id, signed_at_ms, sig}`.
  The gateway **carries it, does not verify** (identical to messages: validate shape +
  bind `origin.client_msg_id` == the frame's `client_msg_id`, persist, echo verbatim;
  absent/garbage = "unverified", never "invalid").
- **Distinct domain tag (security-critical):** reactions sign under
  `aikochat:react:v1:EdDSA`, NOT the message tag `aikochat:msg:v1:EdDSA`. Different
  domain separation so a message signature can never be lifted and re-presented as a
  reaction endorsement (cross-event replay), or vice versa.
- **Canonical signed bytes** (mirror the message SIGNING-SPEC's length-prefixed layout):
  `DOMAIN_TAG ‖ len(channel_id) ‖ len(target_msg_id) ‖ len(emoji) ‖ len(action) ‖ sender_pubkey(32 raw) ‖ client_msg_id ‖ signed_at_ms(u64)`.
  `action` is signed too, so a `remove` (un-vouch) is its own non-repudiable event.
- **Spec + golden vector:** co-authored `docs/crucible/sovereign-reaction-signing/
  SIGNING-SPEC.md` (app-side, like the message spec), pinned by a golden vector; the
  gateway ships a `reaction_signing_bytes()` reconstruction exercised by that vector so
  the two signers can't drift.
- **Reputation caveat:** like messages, a shape-valid signature attests "*some* key
  signed these bytes", NOT "*this account's* key" — the pubkey→account binding is #1816
  PR B (`signing_keys`), not yet landed. Signing now captures the bytes so they become
  reputation-grade retroactively when that trust root lands.

---

## #2632 — @-mention directory + key-bound wire support **[shape mostly final]**

Two pieces. Mentions bind to the **key**, never raw text, so a rename never orphans them.

### 1. Directory lookup — autocomplete **[shape final]**

```
GET /v1/mentions?q=<prefix>&channel=<cid>    → authenticated
```

```json
{ "results": [ { "user_id": "<key>", "handle": "alice", "display_name": "Alice" } ] }
```

- Prefix-matches `handle` and `display_name`. **Channel members first**, then wider
  system. Capped (assume ≤20 results; exact cap [open]).
- `channel` is optional; omit it for a global search (no member-priority ordering).

### 2. Mention spans on a message **[shape final]**

A sent message carries structured spans bound to the key. On the **WS `send` frame** and
the REST send path, add an optional `mentions` array:

```json
{ "type": "send", "client_msg_id": "...", "channel_id": "...", "body": "hi @alice",
  "mentions": [ { "user_id": "<key>", "offset": 3, "length": 6 } ] }
```

- `offset`/`length` index into `body` (the `@alice` run, UTF-16 code units to match
  Dart string indexing — **[confirm]** this indexing basis with me before you ship, it's
  the one shape most likely to bite).
- **Picker-resolved only.** A raw unresolved `@text` the user typed but didn't pick from
  the directory carries **no** span and stays inert forever — the island never
  late-resolves `@text` (a recycled handle would mis-resolve). Only spans you built from
  a directory pick get sent.

`MessageView` echoes them back, **omitted when empty**:

```json
"mentions": [ { "user_id": "<key>", "offset": 3, "length": 6 } ]
```

- **Render:** resolve `user_id` → the *current* handle yourself (via the directory /
  your user cache) at render time, so a later rename shows the new handle. The wire
  carries only the key + span, never a cached handle string.
- **Notification target is the key.** Feeds notifications v0 (#2526) — island-side
  notification work is separate; this handoff only guarantees the span is stored +
  delivered and the target is the key.

---

## #2633 — direct messages as 1:1 channels **[✅ SHIPPED — gateway half on `main`, 2026-08-10]**

> **SHIPPED in PR#124 (squash `a21bba5`), 13-round cage-matched, 1016 tests + CI green.
> NOT deployed yet** (inert until a version bump past 0.5.0 — same as reactions/mentions).
> Design of record: `docs/design/11-direct-messages.md`. This is the FINAL wire + behavior
> contract; build the client against it. Everything below the "shape as first proposed"
> line is unchanged in shape; the **behavior deltas the client MUST handle** are listed
> first because a few evolved during the cage-match.

### What the client MUST handle (behavior, read this before coding the DM UI)

1. **`GET /v1/messages/{id}` lives in the MESSAGES surface, not a DM path** — it's a
   general fetch-one (reply-parent / deep-link), `MessageView | 404` (existence-hiding).
2. **members is ALWAYS an array** (`["<keyA>","<keyB>"]`), member-SET shape (groups later
   are additive). A **self-DM** (`target == me`, allowed) returns `members: ["<me>"]`.
3. **Block → the SEND is REFUSED** (Nick's ruling). Sending to a DM where you are in a
   block relationship with the peer returns a WS `error` frame **`{code: "no_channel"}`** —
   the SAME shape as a missing/unreadable channel (existence-hiding, symmetric in BOTH
   block directions; applies to plain sends AND replies). So: **a `no_channel` on a DM you
   currently hold in `GET /v1/dm` means "can't send" (a block), NOT "the channel vanished".**
   Do not surface it as a hard error; treat it as a soft "message not delivered". Nothing
   is persisted — no residue.
4. **Leave = a resumable DISMISS.** `DELETE /v1/channels/{dm_id}/leave` → `204` drops the DM
   from your `GET /v1/dm`. Re-opening (`POST /v1/dm` with the same peer) resumes it,
   **including any backlog sent while you were away** (unarchive semantics) — it is NOT a
   permanent cutoff and the peer is never re-injected into your roster by their activity.
   **To stop someone contacting you, BLOCK them** (that refuses their sends, even after a
   leave); leave is just "tidy my switcher". (Known caveat: a *live* WS socket keeps
   receiving frames for a left DM until it re-subscribes — a pre-existing hub-lifecycle gap,
   island-tracked; on leave, drop the channel from your local subscription set.)
5. **DMs are excluded from `GET /v1/channels`** — learn your DM channel_ids from
   `GET /v1/dm`, then subscribe to them over WS like any channel.
6. **DMs are island-local** (never federate on the bus) — both members are on the same
   island. Cross-island DMs are a future sealed-federation track, out of scope.
7. **`unread` is client-side** (your `ChannelReadStore` watermark) — the island emits no
   `unread`; `GET /v1/dm` gives `last_message` (a full `MessageView` or `null`) to drive it.

### The big one — shape as first proposed (unchanged, for reference)

DMs reuse the existing channel/message infrastructure — a DM is a channel
with `kind="dm"`, two members, and no community.

**Endpoints** — authenticated:

```
POST /v1/dm    { "target_user_id": "<key>" }    → find-or-create the 1:1 channel
GET  /v1/dm                                      → list my DM channels
```

**`POST /v1/dm`** is **idempotent** — the same unordered pair `{me, target}` always
resolves to the same channel (canonical member ordering under the hood). Returns a
channel view:

```json
{ "channel_id": "<cid>", "kind": "dm", "members": ["<keyA>", "<keyB>"], "created_at": "..." }
```

- `404` if `target_user_id` isn't a real user. Self-DM (`target == me`) → **[open]**
  (allow as a notes-to-self channel, or `400`? assume `400` for now).

**`GET /v1/dm`** — my DM channels for the switcher:

```json
{ "channels": [
  { "channel_id": "<cid>", "kind": "dm", "members": ["<keyA>","<keyB>"],
    "last_message": { "msg_id": "...", "body": "...", "created_at": "...", "sender": {...} } }
] }
```

- `last_message` is the newest visible message (or `null` if none yet).
- **`unread` is NOT promised yet — [open].** There is no `read_positions` mechanism on
  the island (explicitly a later phase). I will not emit a fabricated unread count. If
  you need unread now, it's a separate island task (server-side read cursors); flag it
  and we scope it. Until then, drive unread client-side off your own last-seen if you
  must.

**Messages** flow through the **existing** channel send/receive path — the DM's
`channel_id` in your normal `send` frame and history fetch. **No new message wire type.**

**Authz (island-enforced, you don't police it):** only the two members can read or post;
a DM channel is **excluded from `GET /v1/channels`** (the public list — today it emits
every channel, so this is a real island change) and is not discoverable by anyone else.
`404` (not `403`) for a non-member, to hide existence.

---

## Open items needing a decision from you (or Nick)

1. **#2632 span indexing basis** — UTF-16 code units (Dart-native) vs Unicode
   codepoints vs bytes. Highest-risk shape. Confirm before you ship the composer.
2. **#2633 unread** — not available island-side (no read-position store). Client-side
   for now, or scope a new island task?
3. **#2633 self-DM** — allow (notes-to-self) or `400`?
4. **#2634 `reactors` list** — RESOLVED (app tab): `count` + `reacted_by_me` is enough
   for the UI; `?reactors=1` deferred (additive). Separately RESOLVED: reactions are
   **signed from day one** (see the signing subsection above) — that was the
   irreversible half hiding under this item.
5. **#2632 directory result cap** — 20 assumed.

Reply in-repo (a `HANDOFF-from-app-tab-*.md`) or in the tracker on #2631–2634.
