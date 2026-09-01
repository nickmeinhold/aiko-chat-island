# 🜂 Fold — the author's own pass, before the cold strangers

*Movement 5. No round budget: this is just me. Fold works the metal; it does not
re-grade the ore.*

The first Cast flagged V-2 as *"a factual question about the code that I have not
verified."* Verifying it was the whole value of this movement — it dissolved one step,
shrank another, and corrected a claim I had inherited rather than checked.

---

## F-1 — Step 1 is already built, and Step 2 is one signature, not a mechanism

`realtime/envelopes.py:28`:

```python
def ack(client_msg_id: str, msg_id: str, created_at: str) -> dict:
    return {"type": "ack", "client_msg_id": client_msg_id,
            "msg_id": msg_id, "created_at": created_at}
```

called at `realtime/ws.py:225`:

```python
await conn.send(envelopes.ack(frame["client_msg_id"], row.id, view["created_at"]))
```

**That is the receipt's payload, already computed, already at the right place on the
wire, already delivered to exactly the right party — and merely unsigned.**

The design shrinks accordingly. "Mint a receipt object and plumb it to the client"
becomes **"sign the ack you are already sending."** No new frame, no new delivery path,
no new storage, no new client round-trip. This is the mis-homed-caller corollary firing:
the capability was not missing, it was unsigned.

## F-2 — the position-binding claim is TRUE, but for a narrower reason than the app tab states, and the fix is smaller

I had recorded (from the app tab, and repeated it into Cast 1) that *"`message_view`
carries no frame-level `client_msg_id`, so the check is self-referential."* Checked, and
the picture is more precise:

| Layer | State | Evidence |
|---|---|---|
| Write path | **the binding IS enforced, fail-closed** | `signing.py:297` — `if cmid != frame_client_msg_id: raise OriginError` |
| Storage | **the column exists, and is UNIQUE per channel** | `models.py:545`, and `models.py:531` `UniqueConstraint("channel_id","client_msg_id")` |
| Read path | **the ROW's column is never echoed** | `messages_service.message_view()` (`:48-70`) emits `msg_id, channel_id, sender, body, created_at, reply_to, reactions`, plus `origin`/`mentions` when present — **no `client_msg_id`** |

So the origin envelope carries its *own* self-declared `client_msg_id` (`models.py:549`)
and that IS echoed — which is exactly why the app called the check **self-referential**.
The envelope agrees with itself. Nothing on the read path binds it to the *row's*
`client_msg_id`, so a dishonest island can attach a validly-signed origin to a different
row and no reader can tell.

**The app tab's conclusion is right. Its implied fix was bigger than necessary.** The
minimal correct change is: **echo the row's `client_msg_id` in `message_view`** — the
single serializer through which REST history, WS fanout and bus-ingest fanout all pass.
One field, one function, every read path at once, and it converts an already-enforced
write-time invariant into a reader-checkable one.

This is worth building **whether or not anything else in this bundle survives Temper.**

## F-3 — C-1 (can an island be made to sign?) does not hold, and I am recording it rather than fixing it

The Cast's least-confident claim was that a receipt is something an island can be made
to sign. Folding on it: **it is not.** An operator can strip the signature, return the
old unsigned ack, and be indistinguishable from an island that has not upgraded. There
is no mechanism in this design that makes signing *load-bearing for the island's own
operation* — which is precisely the property Kelvin's spark had and I discarded along
with its public log.

I do not have a fix that survives the privacy constraint. **The honest consequence: the
receipt is evidence when offered, and its absence proves nothing.** That caps the whole
Piece-B conviction story at "works against an island that upgraded and then misbehaved",
not "works against a hostile operator." Cast 2 states this as a named limit rather than
leaving it as a hopeful claim, and Temper should test whether that cap makes Piece B
worth building at all.

## F-4 — degenerate states enumerated (the pre-adversary sweep)

- **n=0 / unsigned messages.** `origin` is legitimately absent for unsigned and
  bus-born rows. A receipt still binds position, so Piece A is *independent* of whether
  the message was signed. Good — it means F-2's fix helps unsigned traffic too.
- **Bus-born rows.** Messages arriving over the bus never had a `client_msg_id` frame
  (the column is `nullable=True`). `message_view` must emit the field as **absent**, not
  `null`, matching the established omit-when-empty contract used for `origin` and
  `mentions` — otherwise `client_msg_id: null` reaches the wire and a client has to
  learn a third state.
- **Idempotent resend.** `UniqueConstraint("channel_id","client_msg_id")` means a
  resent id no-ops to the SAME row. So a second ack for one `client_msg_id` returns the
  same `msg_id` — a signed ack is therefore *replayable but not contradictory*, which is
  the property Piece B needs. Two DIFFERENT `msg_id`s for one `client_msg_id` would be
  the conviction, and the DB constraint makes it structurally hard for an honest island
  to produce accidentally. That is a real strengthening I had not noticed.
- **Concurrent-event.** Ack is sent after the session block exits (`ws.py:225`), using
  values snapshotted inside it. A signature computed at the same point inherits that
  discipline. No new race.
- **NULL `client_msg_id` collision.** SQLite treats NULLs as distinct in a UNIQUE
  constraint, so unlimited bus-born rows coexist. Fine, but it means the uniqueness
  guarantee Piece B leans on **only holds for client-submitted rows**. Stated, not
  hand-waved.

## F-5 — the simplest rejected alternative, tried honestly against my own problem

*Do nothing but Step 0 (show Andy the answer), and build no receipt at all.*

This survives better than I would like. Every engineering step in this bundle is gated
on somebody wanting the evidence, and F-3 says the evidence is uncompellable. Step 0
costs one review pass, involves the person who asked the original question, and unblocks
a two-month-old ticket.

**It does not dissolve F-2**, which is a real defect with a one-field fix and stands on
its own. So the honest floor of this whole forge is: *Step 0 + F-2's field.* Everything
above that has to earn itself at Temper.
