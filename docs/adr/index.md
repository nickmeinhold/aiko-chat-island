# aiko-chat-island — architecture decision records

Decisions that bind **this repo only**. Same two document classes and the same
template as the [app repo's series](../../../aiko_chat_app/docs/adr/index.md):
an ADR records a *decision* — what we chose, why, what we rejected — numbered in
decision order, status `Draft` → `Accepted` / `Rejected` / `Superseded`.

## Where an ADR lives (ruling, Nick, 2026-08-23)

ADRs live in their subject's home repo, the way RFCs already do (RFC-0001 lives
in `aiko_services_dart/docs/rfc/`, because a spec lives with the code that proves
it). The test is **which repos must change if this decision changes:**

| scope | home |
|---|---|
| island only | **here** |
| app only | `aiko_chat_app/docs/adr/` |
| app **and** island | `aiko_chat`, via PR |
| `aiko_services` / protocol | Andy's call |

Existing cross-cutting ADRs (0001, 0002, 0003, 0007 are Andy's and are about
`aiko_services`/`aiko_chat`) currently sit in the app repo by accident of where
they were written. Re-homing them is fine but is not this series' business.

## The series

| ADR | Title | Status | Owner |
|-----|-------|--------|-------|
| 0001 | Alembic is the sole schema authority | Accepted (retroactive) | Nick |
| 0002 | Prod runs SQLite with foreign keys OFF and application-level cascades | Accepted (retroactive) | Nick |
| 0003 | Deploy by pulling a version-pinned published image | Accepted (retroactive) | Nick |

## Why these three first

All three were previously **prose in `CLAUDE.md`** — a directive file, which is
the wrong genre. A directive says *do this*; it cannot say *and here is what we
rejected, and why your instinct to change it is wrong*. Each of these three has
already cost someone real time, and 0002 in particular is a standing invitation
to a well-meaning "fix" that would break parity with production.

`CLAUDE.md` should shrink to pointers at these records rather than restating them.
