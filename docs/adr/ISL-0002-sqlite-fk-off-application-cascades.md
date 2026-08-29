# ISL-0002: Prod runs SQLite with foreign keys OFF and application-level cascades

| | |
|---|---|
| **ADR** | 0002 (island) |
| **Status** | Accepted (retroactive) |
| **Owner** | Nick Meinhold |
| **Created** | 2026-08-23 (documenting a decision in force since the first island) |
| **Thread** | — |

## Summary

An island's store is SQLite, in production, deliberately. `PRAGMA foreign_keys`
is **not** enabled and the schema declares no `ondelete=CASCADE`. Referential
cleanup is performed by service code inside the caller's transaction.

**Do not "fix" this by turning foreign keys on.** Doing so would break parity with
production, not restore it.

## Motivation

An island is a sovereign node someone runs on a small box. A single-file store
with no separate server process is the correct shape for that: it backs up by
copying one file, it has no connection pool to exhaust, and it cannot be
misconfigured into listening on a public port. Dev runs SQLite for the same reason
it runs the same migration chain (ISL-0001) — **so that what you exercise locally
is what production runs.**

Foreign keys being off follows from what the migrations must do. Under ISL-0001,
13 of 21 migrations rebuild a table via `batch_alter_table` — create new, copy,
swap. With `PRAGMA foreign_keys` on, a parent-table swap can trip a child FK
violation mid-migration; with it off, the swap is safe. Several migrations say so
explicitly at the point where it matters (`0002`, `0009`, `0020`).

## Proposal

**Cascading deletes are the service's job, not the database's.** The canonical
example is `channels_service.hard_delete_channel`, which walks
`memberships → messages → channel` inside the caller's transaction and is
deliberately backend-agnostic:

> it does NOT rely on `ondelete=CASCADE` or SQLite's `foreign_keys` pragma (the
> schema has neither), so a raw `DELETE channels` would either IntegrityError on
> Postgres or orphan messages on SQLite.

Two things follow, and both are load-bearing:

1. **Never issue a bare `DELETE` against a parent table.** Go through the service
   that owns the cascade. This is the same "one door" discipline the repo applies
   to trust boundaries: seal the shared mutator, not each caller.
2. **The cascade is written to be correct on both backends**, so the code does not
   silently depend on which one is underneath.

## Rationale and alternatives

- **Why not just enable `PRAGMA foreign_keys`?** It breaks the table-rebuild
  migrations this schema depends on, and it would make correctness depend on a
  connection-level pragma that any new connection can forget to set. A guarantee
  you must remember to switch on is not a guarantee.
- **Why not `ondelete=CASCADE` in the schema?** Same objection one level up: it
  would be inert on SQLite-with-FK-off and active on Postgres, so the two backends
  would have *different delete semantics* while passing the same tests. Silent
  divergence is worse than an explicit cascade in code.
- **Why not Postgres in production?** It makes an island a two-process deployment
  with a backup story that is no longer "copy the file". The cost lands on every
  third-party operator, to buy referential integrity we are already enforcing.

## Unresolved questions

Turning FK enforcement on is not permanently off the table, but it is **blocked on
create-path work** (tracker #1544): rows are currently inserted in orders that an
enforcing backend would reject. Any future move has to fix creation before
deletion — which is why the honest status today is "off on purpose", not "off
because nobody got to it".

## Rejected ideas

- Enabling foreign keys "just in dev" to catch bugs early. This is the
  anti-parity move in its purest form: it makes dev stricter than prod, so dev
  goes red on things production tolerates and green on nothing extra that matters.
