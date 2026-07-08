# Bus Interaction Contract — how the gateway reads and writes aiko state

**Status:** reference, grounded 2026-07-08.
**Grounded on:** `geekscape/aiko_services` `origin/master` `documentation/concepts/`
(v0.7, published PyPI 2026-07-06) — specifically `share.md`, `category.md`,
`hyperspace.md`, `dependency.md` — cross-checked against `geekscape/aiko_chat`
`chat.py` source and Andy Gelme's Signal answers of 2026-07-04.
**Companion to:** [`docs/design/03-auth-on-the-bus.html`](../design/03-auth-on-the-bus.html)
(the *design*); this is the *mechanism reference* that doc points at.
**Visual:** [`aiko-mental-model.html`](aiko-mental-model.html) — a two-panel diagram
of the read/write lanes (panel 1) and why cross-island identity needs a key in the
user entry (panel 2). Open it in a browser; best read alongside this note.

---

## The contract in one line

> **READ** canonical topology by *mirroring* an ECConsumer over an EC share
> (one-way, liveness). **WRITE** (create / update / remove) by *calling* the
> Category Actor's function-call API. Never mutate through the EC control topic.

This is Andy's sanctioned split, verbatim from 2026-07-04: *"EventualConsistency
for observing #channel/@users lists is fine, especially for Dashboards — but
it's a one-way affair, you can't create/update/destroy through an EC client."*

---

## Read side — EventualConsistency (EC) shares

Source: `concepts/share.md`.

- Every **Actor** automatically gets a `self.share` dict and an **ECProducer**
  that publishes every change. A **Category** stores its entries *in its share*
  — which is why `ChatServer` re-exposes channel topology in three lines
  (`chat.py:272-274`):
  ```python
  self.channels      = self.hyperspace.share["entries"]["channels"]
  self.channels_list = self.channels.share["entries"]
  self.share["channel_list"] = self.channels_list
  ```
- The gateway is an **ECConsumer** on `channel_list` (`aiko/client.py`): it holds
  a local replica that converges on the producer's state via an initial snapshot
  + incremental add/update/remove, under a **300-second auto-extending lease**.
- `cache_state` is `"empty"` until the first snapshot completes, then `"ready"`.

### Hard constraints (bite the gateway)

| Constraint | Consequence |
|---|---|
| **EC is one-way.** ECConsumers cannot mutate. | A read mirror can never be the write path. |
| **Share item paths max depth 2** (`a.b.c` raises `ValueError`). | A `user_list` share can't nest arbitrarily; shape it flat. |
| **The lease must be kept alive.** | A stalled consumer loses its subscription; expect re-snapshot on reconnect. |

---

## Write side — the Category function-call API

Source: `concepts/category.md`. `Category(Actor, Dependency)` exposes CRUD on
its collection as **function calls** (invoked remotely as `do_request` over
MQTT — this is what "goes through the API" means):

| Operation | Effect |
|---|---|
| `add(entry_name, service_filter, lcm_url, storage_url)` | Create a Dependency, store under `entry_name` (**no-op if the name exists**) |
| `list(topic_path_response, entry_name, long_format, …)` | Publish Entry records (all, or one) |
| `update(entry_name, service, service_filter, lcm_url, storage_url)` | Merge **non-null** fields into an existing Entry |
| `remove(entry_name)` | Delete the Entry |
| `exit()` | Terminate the Category Actor |

Each **entry is a `Dependency`** — a service reference carrying a `ServiceFilter`
(`name`, `protocol`, `transport`, **`owner`**, `tags`) plus `lcm_url` /
`storage_url`, persisted via the Storage SPI. The **`owner` field is already
present on every entry** — that is the native attach-point for the per-entry
ownership + Category ACLs that design-03 Layer B builds on. It is not something
to add; it is something to start honouring.

### The trap (do not step in it)

`share.md` shows an ECProducer's `topic_control` *does* accept
`(add …)` / `(update …)` / `(remove …)` commands over MQTT
(`mosquitto_pub -t …/control -m "(add count 0)"`). **This is a test /
observability affordance, not the CUD path.** Mutating a Category by poking a
producer control topic bypasses the Category Actor's function-call semantics,
ownership checks, and lifecycle. Sanctioned CUD is the `Category.*` API above.

---

## The `users` gap (#1304 / aiko_chat #6) — sharpened

Design-03 §2b already establishes: there is **no `users` Category** in
`ChatServer` today (TODO at `chat.py:48`), so the old "publish a `user_list` EC
share" framing (aiko_chat issue #6) can't be a clean 3-line mirror of
`channel_list`. This reference adds the *shape* problem underneath it:

> **A Category entry is a `Dependency` — a reference to a distributed
> Service. A user is not a Service.**

So "add a `users` Category symmetric with `channels`" is not merely unbuilt —
it asks the framework to model a *person / account* as a service reference. Two
ways that can resolve, both **Andy's call** (this is the open question on #6):

1. **Users are Dependencies too** — a User entry is a degenerate Dependency
   whose `owner` = the account and whose `ServiceFilter`/`tags` carry the
   stable id + profile. Cheapest; slightly abuses "Dependency = service ref."
2. **Users get a distinct entry type** — Category/HyperSpace grow a non-service
   Entry kind for accounts. Cleaner semantics; more upstream work.

Until one lands, **user existence lives in gateway SQLite**; the HyperSpace
*existence-in-graph / data-in-SQLite* split (design-03) holds, and for users it
is "not even existence yet."

---

## What this feeds

- **#1281** (HyperSpace as source of truth) — this contract is the read/write
  seam that migration crosses.
- **#1304** (user topology read-through) — reframed: blocked on ChatServer
  *populating* a `users` Category (shape TBD above), not on a `user_list` share.
- **#1680** / design-03 (auth on the bus) — Layer B attaches to the `owner`
  field named here.
- **#1712** (vendor OKF docs) — vendor the current `documentation/concepts/`
  (48 docs), not the stale `okf/` snapshot; this note is the EC-vs-API design
  companion that task asked for.
