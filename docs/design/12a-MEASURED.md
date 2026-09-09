# 12a-MEASURED — what the island actually does, measured 2026-09-09

Written because three design documents (island 12, island 12a, app 16 v2) reason in detail
about PushKit and CallKit, and nobody had checked what the island sends. Every claim here is
a grep or a read against `src/` at `c36cfbb`, with the instrument positive-controlled.

## M1. The island has NO VoIP send path. None.

```
$ grep -rni "voip" --include="*.py" src/ | wc -l
2
```

Both hits are **inside one comment** in `domain/apns.py` explaining why we do *not* use VoIP.
There is no VoIP token kind, no `.voip` topic, no PushKit anything.

**Positive control** (because a zero-count grep is a fact about the instrument until proven
otherwise): `grep -c "device" src/aiko_gateway/domain/devices_service.py` → **14**. The
instrument sees the files. The zero is real.

`apns.py:399` sends a hardcoded string literal:

```python
"apns-push-type": "alert",
```

Not a variable, not a parameter, not a branch. **One push type exists.**

## M2. Decision 2's "two token kinds" is designed, not built.

The devices table's enum is `Platform = {APNS, FCM}` — that is **iOS-versus-Android**, not
alert-versus-VoIP. A device row records which *service* to talk to, not which *kind of token*
it holds.

## M3. Consequence — the temper's flaw 5 is PREMATURE, not false.

Flaw 5 (Tesla, seconded by Carnot) says *"a user who declines notifications has a VoIP token
and will never have an alert token, so alert-borne ends cannot reach exactly the phones that
most need the ceiling."*

**That population does not exist today, because no VoIP token exists today.** The hazard is
real and becomes live the moment Decision 2/3 ship. So the finding is correctly aimed and
early — and its disposition (*do not harden Decision 4 yet*) is **strengthened**, not weakened:
you cannot correct a predicate that forks a transport the system has never sent.

**TEMPER.md flaw 5 amended accordingly.**

## M4. Flaw 4's clock collapse contradicts a written rationale in live source. Both are right.

`apns.py:140-145`, beside `_EXPIRATION_SECONDS = 60`:

> 60s is **deliberately** longer than the app's 10s ring-freshness gate: the two clocks answer
> different questions (that one decides whether to RING, this one decides whether the wake is
> still worth delivering at all), and a wake arriving at 30s still usefully says *"you just
> missed something in here."*

Design 12 says the opposite — *"a VoIP push should expire exactly when the ring stops; the two
collapse into one number"* — and the temper adopted that as flaw 4's disposition.

**Neither is wrong. They belong to different architectures.** In the alert world a late wake is
still useful, because the user taps it to enter a room whose door is open. In the VoIP world a
late wake reports a call to CallKit, and a stop that has already fired cannot retract a ring
that has not yet started. **The collapse is correct only under CallKit, and the system is not
running CallKit.**

**TEMPER.md flaw 4 amended:** the collapse is conditional on the CallKit transition, and the
alert-world rationale must be explicitly retired at that point rather than silently
contradicted.

## M5. The live comment still argues the superseded position.

`apns.py:392-398` reasons, in production source:

> Taking PushKit means taking mandatory CallKit with it. Apple's own documented alternative is
> exactly this — a UserNotifications alert — and **it fits "a call is a gathering" better than a
> ring does: a gathering has a door that stays open and needs no 30-second synchronous window.**

That is #3267's decision (2026-08-19), and it was **explicitly superseded** by #3609
(2026-08-29) — *"The alert-push decision's premise no longer holds"*, from Nick: *"I want to get
the app actually ringing, like phone calls ring."* The ordering is clear and there is no
conflict to surface; **but the code still tells the next reader we deliberately chose against
VoIP**, which is the argument they will inherit if they read the source instead of the tracker.

**Owed:** amend the comment to record that the reasoning was correct and is superseded, naming
#3609. Do not delete it — it is the record of a real decision, and the gathering argument may
return if CallKit is abandoned.

## M6. The framing fact none of the three documents states

**Not one VoIP push has ever been sent by this system.** The app tab's `.voipspike` build will
be the first. Every claim in designs 12, 12a and 16 v2 about must-report, the momentary ring,
report-and-end ratios and flaw 9's fleet-wide reputation burn is reasoning about a transport
with **zero operational history here** — Apple's documentation, correctly read, and nothing else.

That is not an argument against the design. It is an argument for why the one-handset
`reportCall(endedAt:)` experiment is not a detail: it is **first contact**, and it is the only
thing standing between three documents and a transport none of them has touched.

Today's working ring — proven on real handsets 2026-08-23 and 2026-08-29 — is an **alert** push
that the user taps. CallKit is not in the picture at all yet.

## What this does not change

- The two contradictions found inside design 12 (Decision 4 vs 1c; the call/not-call predicate
  misnaming) stand — they are about the design's internal consistency, not its transport.
- Nick's 2026-09-09 ruling that the island owns the ring ceiling stands.
- Flaws 1, 2, 3, 6, 7 of TEMPER.md are untouched by these measurements.
