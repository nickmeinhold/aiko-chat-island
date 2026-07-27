# CRUCIBLE — #46 cross-worker active-disconnect

**The ore (verified real):** `Hub._conns` (`realtime/hub.py:36`) is a per-process
`set`, so `disconnect_user` (`hub.py:44`) only closes sockets *this uvicorn worker*
owns. A ban (`rest/moderation.py:225`) or a session gen-bump (`rest/recovery.py:232`)
handled by worker A leaves the target's live WS sockets alive on workers B, C… until
token expiry. The `moderation.py` comment already names the gap — and names its
*assumed* fix ("the redis-fanout path the hub's multi-worker plan names").

**Why this glows (aliveness 2):** the fix is a genuine distributed-systems design
choice, and the obvious/assumed answer (broadcast a disconnect over a bus) is very
likely *not* the best one. The interesting move is that the DB **already holds the
authoritative policy state** (`banned_at`, `token_generation`) and the WS handshake
**already re-checks it at connect** through the PR#96 shared resolver
`auth_session.resolve_session_user`. So the elegant fix isn't "broadcast an event" —
it's "get every worker to periodically **re-derive from the SoT it already trusts**."
That dissolves a whole coupling class (ban→publish, gen-bump→publish, every future
per-user gate→publish) into one mechanism. It's the same lesson as
`sot_symmetric_deletion` and the auth-ingress-fragmentation arc we just shipped.

**Impact (2):** it makes `disconnect_user`'s return count honest under multi-worker,
and it's the *last* thread `concept_auth_ingress_fragmentation` still flags. Not
urgent (moot at 1 worker today) but it's the durable unblock for ever running
`--workers N` / multiple replicas without silently weakening ban + revocation.

**Aliveness × impact = 2 × 2 = 4.** Design-forward, not a toy.

**The spark:** one periodic re-validation sweep, re-using the single door
(`resolve_session_user`'s predicate), enforces ban + gen-bump + *any future per-user
policy* on every worker's open sockets — with **zero new bus topic, zero new wire
format, zero new client-reachable trust surface.**

**The falsifier (what would prove this ore is slag):** if the required cross-worker
latency is *sub-second* (a ban must drop every socket network-wide effectively
instantly), then a periodic sweep's bounded N-second lag is unacceptable and the
bus-fanout (Option A) is genuinely required — the sweep would be the wrong pick and
this framing is slag. Resolve by pinning the actual latency requirement for a ban's
active-disconnect. (Second falsifier: if uvicorn multi-worker is *never* on the
roadmap — SQLite single-writer may cap the island at one process forever — then #46
is permanently moot and the honest output is "won't-fix / document", not a build.)
