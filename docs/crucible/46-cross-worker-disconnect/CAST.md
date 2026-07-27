# CAST — #46 periodic session-reconciliation sweep

> Crucible movement 3 (Cast): the design doc. Follows `CRUCIBLE.md` (Ore + Heat).
> Frame chosen at Heat and greenlit at the two falsifiers: **Option B — re-derive
> from the SoT, don't broadcast an event.** Next movements: Fold (self-adversarial),
> Temper (5-family cage-match on THIS design), Blade (plan mode).

## Problem (verified against the code, 2026-07-27)

`Hub._conns` (`realtime/hub.py:36`) is a per-process `set[Connection]`.
`hub.disconnect_user(user_id)` (`hub.py:44`) closes only the sockets **the calling
uvicorn worker owns**. Two policy-change sites call it —
`rest/moderation.py:225` (ban) and `rest/recovery.py:232` (recovery/gen-bump) — so
under multi-worker, a ban handled by worker A leaves the target's live sockets open
on workers B, C… until access-token expiry. The receive loop
(`ws.py`) authenticates **only at handshake** and never re-checks, so an already-open
socket outlives any policy change that doesn't actively drop it.

### The gap is not purely multi-worker — it is live at one worker today

`disconnect_user` has exactly **two** call sites (grep, whole tree): ban and
recovery. **Account deletion does not call it** (`accounts_service` hard-removes the
row with no `hub` reference, confirmed no call site). So even on today's single
worker, a user who deletes their account while holding an open WS socket keeps that
socket alive with its cached `user` object; the send path derives the sender from
that cached id and never re-loads the row. A hard-deleted user can therefore keep
posting "ghost" messages on the open socket until it naturally drops. Prod runs
FK-off with app-level cascades, so the orphaned-sender insert won't fail on a
constraint. **The sweep closes this present-day gap as a side-effect of re-deriving
from the SoT** — which is the whole argument for Option B: nobody has to *remember*
to wire delete→disconnect, because the sweep asks the DB, and the DB already knows.
*(Fold TODO: confirm the exact post-deletion send behaviour end-to-end; the
architectural point holds regardless of how far a ghost message actually gets.)*

## The two rejected shapes

- **Option A — bus-fanout disconnect.** On every policy change, publish a
  `disconnect(user_id)` event; each worker drops its local sockets on receipt. Costs:
  a new bus topic, a new wire format, a new client-*un*reachable but internally-new
  trust surface, and — critically — a new coupling **per policy type**. Ban must
  publish, gen-bump must publish, delete must publish, every *future* per-user gate
  must remember to publish. It re-creates exactly the auth-ingress fragmentation
  (`concept_auth_ingress_fragmentation`) we just spent PR#96 dissolving, one layer out.
- **Option C — re-check on every received frame.** Re-run the gate in the receive
  loop per inbound message. Rejected: it only catches *active* sockets (a silently-
  idle banned listener is never re-checked), adds a DB round-trip to the hot send
  path, and still needs a separate mechanism for idle sockets.

## Option B — the design

One periodic per-worker task re-derives each open socket's right to exist from the
**single source of truth it already trusts** (the `users` row), using the **same
gate predicate** the handshake uses. Zero new bus topic, zero new wire format, zero
new trust surface. One mechanism enforces ban + gen-bump + deletion + *any future
per-user policy* on open sockets, on every worker.

### 1. Factor the gate into a shared, decode-free predicate (the single door)

`resolve_session_user` (`domain/auth_session.py`) today does **decode → load → gate**
inline. Extract the gate half into a pure function so the sweep can reuse it WITHOUT
decoding a token:

```python
# domain/auth_session.py
def evaluate_session(user: User | None, token_gen: int) -> None:
    """The per-user session policy, decode-free. Raises the same neutral
    exceptions resolve_session_user raises. THE single definition of 'is this
    session still valid' — shared by the token-presenting ingresses (via
    resolve_session_user) and the reconciliation sweep (directly)."""
    if user is None or token_gen != user.token_generation:
        raise InvalidSession
    if users_service.is_banned(user):
        raise SessionBanned
```

`resolve_session_user` becomes `decode → load → evaluate_session(user, token_gen)`.
This is the load-bearing move: the sweep does not re-implement the gate (that would
re-fragment it); it becomes the **fourth consumer** of the one predicate. Any future
per-user gate added to `evaluate_session` is automatically enforced on open sockets.

### 2. Capture the authenticated generation on the Connection

`Connection` (`hub.py`) gains one field. At a successful handshake the resolver has
just proven `token_gen == user.token_generation`, so the current row value **is** the
generation this socket authenticated with:

```python
class Connection:
    def __init__(self, ws, user_id, token_gen: int):
        self.ws = ws
        self.user_id = user_id
        self.token_gen = token_gen        # generation this socket authed with
        self.subscribed: set[str] = set()
```

`ws.py` register site: `Connection(websocket, user.id, user.token_generation)`.
(`user_id` alone covers ban + deletion; `token_gen` is what makes gen-bump/revocation
sweepable without stashing — and re-decoding — the token.)

### 3. The sweep (batched, on the hub)

```python
# hub.py
async def reconcile(self, load_users) -> int:
    """Re-derive every open socket's validity from the SoT. `load_users(ids)`
    returns {id: User|None} in ONE query. Closes sockets whose session no longer
    evaluates. Returns the count closed. Snapshot first — the set mutates under
    concurrent register/unregister/fanout."""
    conns = list(self._conns)                       # snapshot
    if not conns:
        return 0
    users = await load_users({c.user_id for c in conns})
    closed = 0
    for conn in conns:
        try:
            auth_session.evaluate_session(users.get(conn.user_id), conn.token_gen)
        except (auth_session.InvalidSession, auth_session.SessionBanned):
            try:
                await conn.ws.close(code=1008)
            except Exception:
                pass
            self.unregister(conn)
            closed += 1
    return closed
```

Batched by design: **one** `SELECT ... WHERE id IN (:ids)` per sweep over the
*distinct* user_ids, not N+1. The cost that matters is the periodic DB read, and it
is O(distinct users online), bounded and cheap. (`feedback_locate_real_cost`,
`feedback_anticipate_problem_measure_fix` — name the cost, keep it O(online-users).)

### 4. Wire the periodic task in lifespan (established pattern)

`main.py` already runs periodic background tasks this exact way (`_gossip_loop`,
`_run_channel_worker`, cancelled via `contextlib.suppress(asyncio.CancelledError)`
on shutdown). Add one more:

```python
async def _reconcile_loop(state):
    while True:
        try:
            async with SessionLocal() as s:
                async def load(ids): ...  # users_service batch fetch by ids
                closed = await state.hub.reconcile(load)
                if closed:
                    log.info("session sweep: closed %s stale socket(s)", closed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("session sweep failed; continuing")   # never let a
                                                                 # transient DB
                                                                 # error kill the loop
        await asyncio.sleep(settings.session_sweep_interval_seconds)
```

New setting: `session_sweep_interval_seconds` (default proposed **30**; see latency).

## What this deliberately does NOT change

- **The acting worker stays instant.** `moderation.ban` / `recovery.finalize` keep
  their in-process `disconnect_user` call — the user who *did* the ban still sees the
  target's socket drop immediately on their own worker. The sweep is the **cross-
  worker backstop**, not a replacement; it also becomes the *only* mechanism for the
  delete case (which was never wired).
- **No new wire format, topic, or client-reachable surface.** Reads the DB the
  gateway already owns.
- **`resolve_session_user`'s behaviour is unchanged** — it just delegates its gate
  half to `evaluate_session`; the three ingresses render identically.

## Latency semantics (the falsifier, resolved)

Guarantee: acting-worker sockets drop **immediately**; sockets on *other* workers (or
the un-wired delete case) drop within **≤ `session_sweep_interval_seconds`**. Nick
confirmed sub-second network-wide is NOT required, so a bounded seconds-scale lag is
acceptable and Option A's instantaneous fanout is not needed. 30s is a proposed
default trading DB churn against staleness; tunable per island. If a future policy
ever needs sub-second network-wide disconnect, Option A can be *added* for that
policy without removing the sweep (they compose).

## Open questions for Fold / Temper

1. **Sweep interval default** — 30s vs 10s vs 60s. Pure staleness-vs-churn tradeoff;
   measure DB cost at realistic online-user counts before pinning.
2. **Idempotent close race** — between snapshot and `await close`, a conn may be
   unregistered by its own receive loop; closing a dead socket is swallowed, and
   `unregister` is a `discard` (no KeyError). Believed safe; Fold should red-team the
   register/unregister/reconcile interleaving explicitly.
3. **Deletion end-to-end** — verify how far a ghost message from a deleted-but-open
   socket actually gets today (strengthens or trims the "live at one worker" claim).
4. **Should the sweep also cover the acting worker**, making the mutation-site
   `disconnect_user` calls redundant? Argument for keeping both: the mutation-site
   call is the instant path; the sweep is the eventual backstop. Argument for sweep-
   only: one mechanism, less code. Lean: **keep both** (instant matters for the
   acting user's UX), but name it as a real fork for Temper.
5. **Test strategy** — a two-"worker" test needs two `Hub` instances sharing one DB;
   assert a ban committed with only worker-A's `disconnect_user` fired leaves worker
   B's socket open until one `reconcile()`, then closed. RED-prove by asserting the
   socket is open pre-sweep.

## FOLD — author self-adversarial pass (2026-07-27)

Near-zero weight on intent-vs-bytes (I wrote it); full weight on domain-local design
holes I can see. Nine attacks; three landed as refinements, one resolved *for* the
design, the rest held.

### Refinements that change the design (fold these into Blade)

- **F1 — don't hold the DB session across socket-close I/O.** The Cast's
  `_reconcile_loop` opens `SessionLocal()` and calls `reconcile(load)`, which loads
  users AND then `await conn.ws.close()`s each stale socket — network I/O holding a DB
  connection open for no reason. **Split it:** load the `{id: user}` map inside a
  short session, release it, THEN run the evaluate+close loop with no session held.
  `reconcile` should receive the already-loaded map (or a load-then-release helper),
  not do network closes inside the session block. Cheap, and it stops a slow client
  socket-close from pinning a DB connection.
- **F2 — keep the policy predicate OUT of `hub.py`; inject it.** `Hub`'s docstring is
  "in-process connection registry + channel fanout" — a pure infrastructure registry
  with zero domain knowledge. Importing `domain.auth_session` into `hub.py` to call
  `evaluate_session` inverts that. Instead `reconcile(users, is_valid)` takes the
  predicate as a callback (same shape as passing `load_users`); `main.py`'s loop —
  which already imports `domain` freely — supplies `evaluate_session`. Hub stays a
  registry that knows *how to close a socket*, not *when a session is invalid*.
- **F3 — cite the proven cross-task-close precedent, still red-team interleaving.**
  The sweep closes a socket from a task *other* than the one awaiting
  `websocket.receive_json()`. That exact cross-task close is already what shipped-and-
  cage-matched `disconnect_user` does (ban request task closes while the WS receive
  loop runs elsewhere), so the pattern is accepted here — de-risks the concurrency
  question. Temper should still explicitly interleave register/unregister/reconcile.

### Attack that resolved FOR the design (a property, not a bug)

- **TOCTOU on the captured generation is correct behaviour.** `conn.token_gen` is read
  from the `user` object the resolver loaded, captured at register time *after* the
  resolver's session closed. If a gen-bump commits in the window between resolve and
  register, the detached object carries the OLD gen — and the sweep then closes the
  socket next round (captured-old ≠ row-new). That is exactly right: a socket that
  authenticated against a now-superseded generation *should* die. The capture is
  honest about "what this socket authed with," and the sweep enforces "is that still
  current." No guard needed.

### Attacks that held (no change)

- Deleted user → batch load omits the id → `.get()` returns `None` →
  `evaluate_session(None, gen)` raises `InvalidSession` → closed. Correct.
- Idempotent close/unregister: `unregister` is `set.discard` (no KeyError on double);
  `ws.close()` on an already-closed socket is swallowed. Double-drop (sweep + receive
  loop `finally`) is safe.
- Socket registered mid-sweep is simply skipped this round — it was validated at its
  own handshake ms ago, covered next round. No leak.
- Standing cost: one `SELECT ... IN (:online_user_ids)` per worker per interval,
  O(online users) — negligible at island scale even with per-worker phase overlap.

### Fold verdict

Design is sound; F1/F2 are mechanical hygiene folded into the plan, F3 is a de-risk.
The one thing only a *different-family* Temper can pressure that I cannot: whether the
whole build is justified *now* given it is mostly future-proofing + one small live
gap — i.e. Ore-slag-check #2 (is it worth building vs document-and-wait), which an
author instance is biased to answer "yes, it's elegant." That is the question to hand
the five families.
```
